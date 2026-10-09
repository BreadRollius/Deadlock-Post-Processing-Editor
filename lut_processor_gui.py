"""
LUT Processor GUI - Enhanced Color Grading Tool
A GUI application for modifying LUT data in .vpost files with real-time preview.
"""

import numpy as np
from PIL import Image, ImageTk
import re
import os
import sys
import shutil
import fnmatch
import glob
import colorsys
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import threading
import subprocess

try:
    import vpk
    VPK_AVAILABLE = True
except ImportError:
    VPK_AVAILABLE = False


def find_csdk_path(app_dir):
    """Return the first CSDK directory containing the Source 2 resource compiler."""
    candidates = [
        os.environ.get("DEADLOCK_CSDK_PATH"),
        os.path.join(app_dir, "CSDK"),
        os.path.join(app_dir, "Reduced_CSDK_12"),
        r"F:\Reduced_CSDK_12",
        r"C:\Reduced_CSDK_12",
    ]

    for candidate in candidates:
        if not candidate:
            continue
        compiler = os.path.join(candidate, "game", "bin_tools", "win64", "resourcecompiler.exe")
        if os.path.isfile(compiler):
            return os.path.normpath(candidate)
    return ""


def find_deadlock_path():
    """Find a Deadlock installation in common Steam library locations."""
    configured_path = os.environ.get("DEADLOCK_GAME_PATH")
    if configured_path and os.path.isdir(configured_path):
        return os.path.normpath(configured_path)

    steam_roots = [
        os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "Steam"),
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Steam"),
    ]

    if sys.platform == "win32":
        try:
            import winreg
            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                for key_name in (r"Software\Valve\Steam", r"Software\WOW6432Node\Valve\Steam"):
                    try:
                        with winreg.OpenKey(hive, key_name) as key:
                            steam_roots.append(winreg.QueryValueEx(key, "SteamPath")[0])
                    except OSError:
                        pass
        except ImportError:
            pass

    library_roots = list(steam_roots)
    for steam_root in steam_roots:
        libraries_file = os.path.join(steam_root, "steamapps", "libraryfolders.vdf")
        try:
            with open(libraries_file, "r", encoding="utf-8") as handle:
                for match in re.finditer(r'"path"\s+"([^"]+)"', handle.read()):
                    library_roots.append(match.group(1).replace(r"\\", "\\"))
        except OSError:
            pass

    for library_root in dict.fromkeys(os.path.normpath(path) for path in library_roots):
        candidate = os.path.join(library_root, "steamapps", "common", "Deadlock")
        if os.path.isdir(candidate):
            return candidate
    return ""

# ============================================================================
# LUT PROCESSING ENGINE (from original lut_processor.py)
# ============================================================================

def parse_vpost_file(filepath):
    """Parse a .vpost file and extract header, LUT values, and footer."""
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    lut_start_pattern = r'm_lut\s*=\s*\['
    
    lut_start_match = re.search(lut_start_pattern, content)
    if not lut_start_match:
        raise ValueError("Could not find 'm_lut = [' in the vpost file")
    
    header_end = lut_start_match.end()
    header = content[:header_end]
    
    remaining_content = content[header_end:]
    
    bracket_depth = 1
    lut_data_end = -1
    
    for i, char in enumerate(remaining_content):
        if char == '[':
            bracket_depth += 1
        elif char == ']':
            bracket_depth -= 1
            if bracket_depth == 0:
                lut_data_end = header_end + i
                break
    
    if lut_data_end == -1:
        raise ValueError("Could not find closing bracket for m_lut array")
    
    lut_section = content[header_end:lut_data_end]
    lut_values = []
    for match in re.finditer(r'([\d.]+)', lut_section):
        try:
            lut_values.append(float(match.group(1)))
        except ValueError:
            pass
    
    footer = content[lut_data_end:]
    
    return header, lut_values, footer

def parse_raw_file(filepath):
    """Parse a .raw binary LUT file (uint8 format, 32^3 entries)."""
    data = np.fromfile(filepath, dtype=np.uint8)
    
    # Reshape to RGB entries
    if len(data) % 3 != 0:
        raise ValueError(f"Raw file size {len(data)} is not divisible by 3")
    
    rgb_data = data.reshape(-1, 3)
    
    # Determine LUT dimension (should be a perfect cube)
    lut_size = len(rgb_data)
    lut_dim = round(lut_size ** (1/3))
    
    if lut_dim ** 3 != lut_size:
        raise ValueError(f"LUT size {lut_size} is not a perfect cube")
    
    # Convert to float 0-1 range
    lut_rgb = rgb_data.astype(np.float32) / 255.0
    
    return lut_rgb, lut_dim

def get_luminance(lut_rgb):
    return 0.2126 * lut_rgb[:, 0] + 0.7152 * lut_rgb[:, 1] + 0.0722 * lut_rgb[:, 2]

def apply_gamma(lut_rgb, gamma_value):
    if gamma_value == 1.0:
        return lut_rgb
    # Invert gamma so higher values = brighter (more intuitive)
    inverted_gamma = 1.0 / gamma_value
    return np.power(np.clip(lut_rgb, 0.0001, None), inverted_gamma)

def apply_tint(lut_rgb, r, g, b):
    if r == 1.0 and g == 1.0 and b == 1.0:
        return lut_rgb
    tint_multiplier = np.array([r, g, b], dtype=np.float32)
    return lut_rgb * tint_multiplier

def apply_exposure(lut_rgb, stops):
    if stops == 0.0:
        return lut_rgb
    multiplier = pow(2.0, stops)
    return lut_rgb * multiplier

def apply_saturation(lut_rgb, sat):
    if sat == 1.0:
        return lut_rgb
    
    lum = get_luminance(lut_rgb).reshape(-1, 1)
    grayscale = np.repeat(lum, 3, axis=1)
    return grayscale + sat * (lut_rgb - grayscale)

def apply_vibrance(lut_rgb, amount):
    if amount == 0.0:
        return lut_rgb
    
    rgb_max = np.max(lut_rgb, axis=1)
    rgb_avg = np.mean(lut_rgb, axis=1)
    
    current_sat = rgb_max - rgb_avg
    
    lum = get_luminance(lut_rgb).reshape(-1, 1)
    current_sat = current_sat.reshape(-1, 1)
    boost = amount * (1.0 - current_sat) 
    
    return lut_rgb + (lut_rgb - lum) * boost

def apply_brightness(lut_rgb, brightness_value):
    if brightness_value == 0.0:
        return lut_rgb
    return lut_rgb + brightness_value

def apply_contrast(lut_rgb, contrast_value):
    if contrast_value == 1.0:
        return lut_rgb
    midpoint = 0.5
    return (lut_rgb - midpoint) * contrast_value + midpoint

def apply_temperature(lut_rgb, temp):
    if temp == 0.0:
        return lut_rgb
    
    if temp > 0:
        r_adjust = 1.0 + temp
        g_adjust = 1.0 + temp * 0.5
        b_adjust = 1.0 - temp * 0.5
    else:
        r_adjust = 1.0 + temp * 0.5
        g_adjust = 1.0 + temp * 0.25
        b_adjust = 1.0 - temp
    
    temp_multiplier = np.array([r_adjust, g_adjust, b_adjust], dtype=np.float32)
    return lut_rgb * temp_multiplier

def apply_hue_shift(lut_rgb, shift):
    if shift == 0.0:
        return lut_rgb
        
    N = len(lut_rgb)
    output = np.zeros_like(lut_rgb)
    
    for i in range(N):
        r, g, b = lut_rgb[i]
        h, s, v = colorsys.rgb_to_hsv(max(0, min(1, r)), max(0, min(1, g)), max(0, min(1, b)))
        h = (h + shift) % 1.0
        r_n, g_n, b_n = colorsys.hsv_to_rgb(h, s, v)
        output[i] = [r_n, g_n, b_n]
        
    return output

def apply_shadow_highlight_tint(lut_rgb, s_tint, h_tint):
    s_r, s_g, s_b = s_tint
    h_r, h_g, h_b = h_tint
    
    if (s_r == 1.0 and s_g == 1.0 and s_b == 1.0 and 
        h_r == 1.0 and h_g == 1.0 and h_b == 1.0):
        return lut_rgb
    
    lum = get_luminance(lut_rgb).reshape(-1, 1)
    
    s_mask = np.maximum(0, (1.0 - lum) ** 2) 
    h_mask = np.maximum(0, lum ** 2)
    
    s_vec = np.array([s_r, s_g, s_b], dtype=np.float32)
    h_vec = np.array([h_r, h_g, h_b], dtype=np.float32)
    
    if s_r != 1.0 or s_g != 1.0 or s_b != 1.0:
        factor = (s_vec * s_mask) + (1.0 - s_mask)
        lut_rgb = lut_rgb * factor
        
    if h_r != 1.0 or h_g != 1.0 or h_b != 1.0:
        factor = (h_vec * h_mask) + (1.0 - h_mask)
        lut_rgb = lut_rgb * factor
        
    return lut_rgb

def apply_midtone_tint(lut_rgb, m_r, m_g, m_b):
    """Apply tint to midtones only."""
    if m_r == 1.0 and m_g == 1.0 and m_b == 1.0:
        return lut_rgb
    
    lum = get_luminance(lut_rgb).reshape(-1, 1)
    
    # Midtone mask - peaks at 0.5 luminance
    m_mask = 4 * lum * (1.0 - lum)  # Bell curve centered at 0.5
    
    m_vec = np.array([m_r, m_g, m_b], dtype=np.float32)
    factor = (m_vec * m_mask) + (1.0 - m_mask)
    
    return lut_rgb * factor

def apply_levels(lut_rgb, blacks, whites, fade):
    """Apply levels adjustments: blacks lift, whites clip, and fade."""
    if blacks == 0.0 and whites == 1.0 and fade == 0.0:
        return lut_rgb
    
    lut = lut_rgb.copy()
    
    # Fade: raise the black point (washed out look)
    if fade > 0.0:
        lut = lut * (1.0 - fade) + fade
    
    # Blacks: lift the black point
    if blacks > 0.0:
        lut = lut * (1.0 - blacks) + blacks
    
    # Whites: lower the white point
    if whites < 1.0:
        lut = lut * whites
    
    return np.clip(lut, 0.0, 1.0)

def apply_rainbow(lut_rgb, strength):
    if strength == 0.0:
        return lut_rgb
    
    N = len(lut_rgb)
    output = lut_rgb.copy()
    for i in range(N):
        t = i / (N - 1)
        r_h, g_h, b_h = colorsys.hsv_to_rgb(t, 1.0, 1.0)
        rainbow_color = np.array([r_h, g_h, b_h], dtype=np.float32)
        output[i] = (1 - strength) * lut_rgb[i] + strength * rainbow_color
    
    return output

def process_lut(lut_rgb, params):
    """Apply all color grading effects based on parameters."""
    lut = lut_rgb.copy()
    
    # Exposure first
    if params['exposure'] != 0.0:
        lut = apply_exposure(lut, params['exposure'])
    
    # Levels (blacks, whites, fade)
    if params.get('blacks', 0.0) != 0.0 or params.get('whites', 1.0) != 1.0 or params.get('fade', 0.0) != 0.0:
        lut = apply_levels(lut, params.get('blacks', 0.0), params.get('whites', 1.0), params.get('fade', 0.0))
        
    if params['hue_shift'] != 0.0:
        lut = apply_hue_shift(lut, params['hue_shift'])

    if params['tint_r'] != 1.0 or params['tint_g'] != 1.0 or params['tint_b'] != 1.0:
        lut = apply_tint(lut, params['tint_r'], params['tint_g'], params['tint_b'])
        
    if params['temperature'] != 0.0:
        lut = apply_temperature(lut, params['temperature'])
        
    # Shadow/Highlight/Midtone tinting
    shadow_tint = [params['shadow_r'], params['shadow_g'], params['shadow_b']]
    highlight_tint = [params['highlight_r'], params['highlight_g'], params['highlight_b']]
    if (shadow_tint != [1.0, 1.0, 1.0] or highlight_tint != [1.0, 1.0, 1.0]):
        lut = apply_shadow_highlight_tint(lut, shadow_tint, highlight_tint)
    
    # Midtone tint
    if params.get('midtone_r', 1.0) != 1.0 or params.get('midtone_g', 1.0) != 1.0 or params.get('midtone_b', 1.0) != 1.0:
        lut = apply_midtone_tint(lut, params.get('midtone_r', 1.0), params.get('midtone_g', 1.0), params.get('midtone_b', 1.0))

    if params['vibrance'] != 0.0:
        lut = apply_vibrance(lut, params['vibrance'])

    if params['saturation'] != 1.0:
        lut = apply_saturation(lut, params['saturation'])
    
    if params['contrast'] != 1.0:
        lut = apply_contrast(lut, params['contrast'])

    if params['brightness'] != 0.0:
        lut = apply_brightness(lut, params['brightness'])
        
    if params['gamma'] != 1.0:
        lut = apply_gamma(lut, params['gamma'])
    
    if params['invert']:
        lut = 1.0 - lut
    
    if params['rainbow'] > 0.0:
        lut = apply_rainbow(lut, params['rainbow'])
    
    return np.clip(lut, 0.0, 1.0)

def apply_lut_to_image(image_array, lut_rgb, lut_dim=32):
    """Apply the 3D LUT to an image efficiently.
    
    LUT is stored as flat array where R varies fastest, then G, then B.
    So the flat index is: b * 32 * 32 + g * 32 + r
    """
    # The LUT file is stored with R varying fastest, then G, then B
    # So we need to reshape as (B, G, R, channels) to get correct indexing
    lut_3d = lut_rgb.reshape((lut_dim, lut_dim, lut_dim, 3))
    
    # Normalize pixel values to LUT coordinate space
    h, w, c = image_array.shape
    pixels = image_array.astype(np.float32) / 255.0
    
    # Scale to LUT indices
    scaled = pixels * (lut_dim - 1)
    
    # Get integer indices and fractional parts for trilinear interpolation
    idx_low = np.floor(scaled).astype(np.int32)
    idx_high = np.minimum(idx_low + 1, lut_dim - 1)
    frac = scaled - idx_low
    
    # Clamp indices
    idx_low = np.clip(idx_low, 0, lut_dim - 1)
    idx_high = np.clip(idx_high, 0, lut_dim - 1)
    
    # For the LUT stored as (B, G, R, 3):
    # - First dimension indexes by Blue
    # - Second dimension indexes by Green  
    # - Third dimension indexes by Red
    b_lo, g_lo, r_lo = idx_low[:,:,2], idx_low[:,:,1], idx_low[:,:,0]
    b_hi, g_hi, r_hi = idx_high[:,:,2], idx_high[:,:,1], idx_high[:,:,0]
    r_f, g_f, b_f = frac[:,:,0:1], frac[:,:,1:2], frac[:,:,2:3]
    
    # 8 corner samples - index as [B, G, R]
    c000 = lut_3d[b_lo, g_lo, r_lo]
    c001 = lut_3d[b_lo, g_lo, r_hi]
    c010 = lut_3d[b_lo, g_hi, r_lo]
    c011 = lut_3d[b_lo, g_hi, r_hi]
    c100 = lut_3d[b_hi, g_lo, r_lo]
    c101 = lut_3d[b_hi, g_lo, r_hi]
    c110 = lut_3d[b_hi, g_hi, r_lo]
    c111 = lut_3d[b_hi, g_hi, r_hi]
    
    # Trilinear interpolation
    c00 = c000 * (1 - r_f) + c001 * r_f
    c01 = c010 * (1 - r_f) + c011 * r_f
    c10 = c100 * (1 - r_f) + c101 * r_f
    c11 = c110 * (1 - r_f) + c111 * r_f
    
    c0 = c00 * (1 - g_f) + c01 * g_f
    c1 = c10 * (1 - g_f) + c11 * g_f
    
    result = c0 * (1 - b_f) + c1 * b_f
    
    return (np.clip(result, 0, 1) * 255).astype(np.uint8)


# ============================================================================
# GUI APPLICATION
# ============================================================================

class LUTProcessorGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("LUT Processor - Color Grading Tool")
        self.root.geometry("1400x900")
        self.root.minsize(1200, 800)
        
        # Configure modern dark theme colors
        self.colors = {
            'bg_dark': '#0d0d0d',      # Near black background
            'bg_medium': '#1a1a1a',    # Dark gray panels
            'bg_light': '#2d2d2d',     # Lighter gray for inputs
            'bg_card': '#242424',      # Card/section background
            'accent': '#6366f1',       # Indigo accent (modern)
            'accent_hover': '#818cf8', # Lighter indigo on hover
            'accent_secondary': '#22c55e',  # Green for success/export
            'text': '#f5f5f5',         # Almost white text
            'text_dim': '#888888',     # Dimmed text
            'text_label': '#b3b3b3',   # Label text
            'border': '#333333',       # Subtle borders
            'slider_trough': '#3d3d3d', # Slider background
            'slider_active': '#6366f1', # Slider active color
            'red': '#ef4444',          # For RGB sliders
            'green': '#22c55e',        # For RGB sliders
            'blue': '#3b82f6',         # For RGB sliders
        }
        
        self.root.configure(bg=self.colors['bg_dark'])
        
        # Configure styles
        self.setup_styles()
        
        # Data
        self.input_path = tk.StringVar()
        self.output_dir = tk.StringVar()
        self.preview_image = None
        self.preview_image_original = None
        self.preview_image_array = None
        self.original_lut = None
        self.header = None
        self.footer = None
        self.lut_dim = 32
        
        # Set default paths (relative to app location for easy distribution)
        if getattr(sys, 'frozen', False):
            # Running as compiled exe
            app_dir = os.path.dirname(sys.executable)
        else:
            # Running as script
            app_dir = os.path.dirname(os.path.abspath(__file__))
        
        # Default paths relative to app directory
        default_input = os.path.join(app_dir, "Example", "basepostprocess_deadlock.raw")
        default_preview = os.path.join(app_dir, "Base LUT", "No Post Processing New.png")
        default_output = os.path.join(app_dir, "Outputs")
        
        # Create Outputs folder if it doesn't exist
        os.makedirs(default_output, exist_ok=True)
        
        if os.path.exists(default_input):
            self.input_path.set(default_input)
        self.output_dir.set(default_output)
        self.app_dir = app_dir  # Store for later use
        self.is_raw_file = False
        
        # Parameter variables with defaults
        self.params = {
            # Basic adjustments
            'gamma': tk.DoubleVar(value=1.0),
            'exposure': tk.DoubleVar(value=0.0),
            'brightness': tk.DoubleVar(value=0.0),
            'contrast': tk.DoubleVar(value=1.0),
            # Levels
            'blacks': tk.DoubleVar(value=0.0),      # Raise black point (film look)
            'whites': tk.DoubleVar(value=1.0),      # Lower white point
            'fade': tk.DoubleVar(value=0.0),        # Washed out look
            # Color
            'saturation': tk.DoubleVar(value=1.0),
            'vibrance': tk.DoubleVar(value=0.0),
            'temperature': tk.DoubleVar(value=0.0),
            'hue_shift': tk.DoubleVar(value=0.0),
            # Global tint
            'tint_r': tk.DoubleVar(value=1.0),
            'tint_g': tk.DoubleVar(value=1.0),
            'tint_b': tk.DoubleVar(value=1.0),
            # Shadow tint
            'shadow_r': tk.DoubleVar(value=1.0),
            'shadow_g': tk.DoubleVar(value=1.0),
            'shadow_b': tk.DoubleVar(value=1.0),
            # Highlight tint
            'highlight_r': tk.DoubleVar(value=1.0),
            'highlight_g': tk.DoubleVar(value=1.0),
            'highlight_b': tk.DoubleVar(value=1.0),
            # Midtone tint (new)
            'midtone_r': tk.DoubleVar(value=1.0),
            'midtone_g': tk.DoubleVar(value=1.0),
            'midtone_b': tk.DoubleVar(value=1.0),
            # Effects
            'invert': tk.BooleanVar(value=False),
            'rainbow': tk.DoubleVar(value=0.0),
        }
        
        # Default values for reset
        self.defaults = {k: v.get() for k, v in self.params.items()}
        
        # Build UI
        self.build_ui()
        
        # Load default preview image
        if os.path.exists(default_preview):
            self.load_preview_image(default_preview)
        
        # Load default LUT file
        if self.input_path.get() and os.path.exists(self.input_path.get()):
            self.load_lut_file()
        
        # Update timer for debounced preview
        self.update_scheduled = False
        
    def setup_styles(self):
        """Configure ttk styles for dark theme."""
        style = ttk.Style()
        style.theme_use('clam')
        
        # Configure colors
        style.configure('Dark.TFrame', background=self.colors['bg_dark'])
        style.configure('Medium.TFrame', background=self.colors['bg_medium'])
        style.configure('Light.TFrame', background=self.colors['bg_light'])
        
        style.configure('Dark.TLabel', 
                       background=self.colors['bg_dark'],
                       foreground=self.colors['text'],
                       font=('Segoe UI', 10))
        
        style.configure('Title.TLabel',
                       background=self.colors['bg_dark'],
                       foreground=self.colors['accent'],
                       font=('Segoe UI', 14, 'bold'))
        
        style.configure('Section.TLabel',
                       background=self.colors['bg_medium'],
                       foreground=self.colors['text'],
                       font=('Segoe UI', 11, 'bold'))
        
        style.configure('Value.TLabel',
                       background=self.colors['bg_medium'],
                       foreground=self.colors['accent'],
                       font=('Segoe UI Semibold', 9))
        
        style.configure('Accent.TButton',
                       background=self.colors['accent'],
                       foreground='white',
                       font=('Segoe UI', 10, 'bold'),
                       padding=(15, 8))
        
        style.map('Accent.TButton',
                 background=[('active', self.colors['accent_hover'])])
        
        style.configure('Dark.TButton',
                       background=self.colors['bg_light'],
                       foreground=self.colors['text'],
                       font=('Segoe UI', 9),
                       padding=(10, 5))
        
        style.configure('Dark.TEntry',
                       fieldbackground=self.colors['bg_medium'],
                       foreground=self.colors['text'],
                       insertcolor=self.colors['text'])
        
        # Configure Scale (slider) style
        style.configure('Dark.Horizontal.TScale',
                       background=self.colors['bg_medium'],
                       troughcolor=self.colors['slider_trough'],
                       sliderthickness=16,
                       sliderlength=20)
        
        style.configure('Dark.TCheckbutton',
                       background=self.colors['bg_medium'],
                       foreground=self.colors['text'],
                       font=('Segoe UI', 10))
        
    def build_ui(self):
        """Build the main user interface."""
        # Main container with padding
        main_frame = tk.Frame(self.root, bg=self.colors['bg_dark'])
        main_frame.pack(fill='both', expand=True, padx=20, pady=15)
        
        # Header bar
        header_frame = tk.Frame(main_frame, bg=self.colors['bg_dark'])
        header_frame.pack(fill='x', pady=(0, 15))
        
        # App title with icon
        title_container = tk.Frame(header_frame, bg=self.colors['bg_dark'])
        title_container.pack(side='left')
        
        title = tk.Label(title_container, text="LUT Processor",
                        bg=self.colors['bg_dark'],
                        fg=self.colors['text'],
                        font=('Segoe UI', 20, 'bold'))
        title.pack(side='left')
        
        subtitle = tk.Label(title_container, text="  Color Grading Tool",
                           bg=self.colors['bg_dark'],
                           fg=self.colors['text_dim'],
                           font=('Segoe UI', 12))
        subtitle.pack(side='left', pady=(6, 0))
        
        # Content area (left: controls, right: preview)
        content_frame = tk.Frame(main_frame, bg=self.colors['bg_dark'])
        content_frame.pack(fill='both', expand=True)
        
        # Left panel - Controls (fixed width)
        left_panel = tk.Frame(content_frame, bg=self.colors['bg_dark'], width=420)
        left_panel.pack(side='left', fill='y', padx=(0, 15))
        left_panel.pack_propagate(False)  # Maintain fixed width
        
        # File selection section
        self.build_file_section(left_panel)
        
        # Scrollable parameters section
        self.build_parameters_section(left_panel)
        
        # Action buttons
        self.build_action_buttons(left_panel)
        
        # Right panel - Preview with border
        right_panel = tk.Frame(content_frame, bg=self.colors['bg_card'],
                              highlightbackground=self.colors['border'],
                              highlightthickness=1)
        right_panel.pack(side='right', fill='both', expand=True)
        
        self.build_preview_section(right_panel)
        
    def build_file_section(self, parent):
        """Build file selection UI with modern card style."""
        # Card container
        file_frame = tk.Frame(parent, bg=self.colors['bg_card'],
                             highlightbackground=self.colors['border'],
                             highlightthickness=1)
        file_frame.pack(fill='x', pady=(0, 10))
        
        # Section title
        title_label = tk.Label(file_frame, text="FILE SETTINGS",
                              bg=self.colors['bg_card'],
                              fg=self.colors['text_dim'],
                              font=('Segoe UI', 9, 'bold'))
        title_label.pack(anchor='w', padx=12, pady=(12, 8))
        
        # Input file row
        input_container = tk.Frame(file_frame, bg=self.colors['bg_card'])
        input_container.pack(fill='x', padx=12, pady=(0, 8))
        
        input_label = tk.Label(input_container, text="LUT File",
                              bg=self.colors['bg_card'],
                              fg=self.colors['text_label'],
                              font=('Segoe UI', 9))
        input_label.pack(anchor='w')
        
        input_row = tk.Frame(input_container, bg=self.colors['bg_card'])
        input_row.pack(fill='x', pady=(4, 0))
        
        input_entry = tk.Entry(input_row, textvariable=self.input_path,
                              bg=self.colors['bg_light'], fg=self.colors['text'],
                              insertbackground=self.colors['text'], font=('Segoe UI', 9),
                              relief='flat', highlightthickness=1,
                              highlightbackground=self.colors['border'],
                              highlightcolor=self.colors['accent'])
        input_entry.pack(side='left', fill='x', expand=True, ipady=6, padx=(0, 8))
        
        browse_btn = tk.Button(input_row, text="Browse", 
                              bg=self.colors['bg_light'], fg=self.colors['text'],
                              font=('Segoe UI', 9), relief='flat', cursor='hand2',
                              activebackground=self.colors['accent'],
                              activeforeground='white',
                              command=self.browse_input)
        browse_btn.pack(side='right', ipadx=12, ipady=4)
        
        # Output directory row
        output_container = tk.Frame(file_frame, bg=self.colors['bg_card'])
        output_container.pack(fill='x', padx=12, pady=(0, 12))
        
        output_label = tk.Label(output_container, text="Output Directory",
                               bg=self.colors['bg_card'],
                               fg=self.colors['text_label'],
                               font=('Segoe UI', 9))
        output_label.pack(anchor='w')
        
        output_row = tk.Frame(output_container, bg=self.colors['bg_card'])
        output_row.pack(fill='x', pady=(4, 0))
        
        output_entry = tk.Entry(output_row, textvariable=self.output_dir,
                               bg=self.colors['bg_light'], fg=self.colors['text'],
                               insertbackground=self.colors['text'], font=('Segoe UI', 9),
                               relief='flat', highlightthickness=1,
                               highlightbackground=self.colors['border'],
                               highlightcolor=self.colors['accent'])
        output_entry.pack(side='left', fill='x', expand=True, ipady=6, padx=(0, 8))
        
        browse_out_btn = tk.Button(output_row, text="Browse",
                                  bg=self.colors['bg_light'], fg=self.colors['text'],
                                  font=('Segoe UI', 9), relief='flat', cursor='hand2',
                                  activebackground=self.colors['accent'],
                                  activeforeground='white',
                                  command=self.browse_output)
        browse_out_btn.pack(side='right', ipadx=12, ipady=4)
        
    def build_parameters_section(self, parent):
        """Build the scrollable parameters panel."""
        # Create a frame with explicit scrolling
        outer_frame = tk.Frame(parent, bg=self.colors['bg_medium'])
        outer_frame.pack(fill='both', expand=True, pady=(0, 10))
        
        # Create canvas and scrollbar
        self.param_canvas = tk.Canvas(outer_frame, bg=self.colors['bg_medium'], 
                                      highlightthickness=0, width=380)
        scrollbar = tk.Scrollbar(outer_frame, orient='vertical', 
                                command=self.param_canvas.yview)
        
        # Create inner frame for parameters
        self.params_frame = tk.Frame(self.param_canvas, bg=self.colors['bg_medium'])
        
        # Configure scrollbar
        self.param_canvas.configure(yscrollcommand=scrollbar.set)
        
        # Pack scrollbar and canvas
        scrollbar.pack(side='right', fill='y')
        self.param_canvas.pack(side='left', fill='both', expand=True)
        
        # Create window in canvas
        self.canvas_window = self.param_canvas.create_window((0, 0), 
                                                              window=self.params_frame, 
                                                              anchor='nw',
                                                              width=380)
        
        # Configure scroll region when params_frame changes size
        def on_frame_configure(event):
            self.param_canvas.configure(scrollregion=self.param_canvas.bbox('all'))
        
        def on_canvas_configure(event):
            # Update the window width to match canvas
            self.param_canvas.itemconfig(self.canvas_window, width=event.width)
        
        self.params_frame.bind('<Configure>', on_frame_configure)
        self.param_canvas.bind('<Configure>', on_canvas_configure)
        
        # Enable mousewheel scrolling - bind globally when mouse is in the panel
        def on_mousewheel(event):
            self.param_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        
        # Bind/unbind mousewheel when entering/leaving the entire outer frame
        def on_enter(event):
            outer_frame.bind_all('<MouseWheel>', on_mousewheel)
        
        def on_leave(event):
            outer_frame.unbind_all('<MouseWheel>')
        
        outer_frame.bind('<Enter>', on_enter)
        outer_frame.bind('<Leave>', on_leave)
        
        # Build parameter controls - now using tk.Frame for consistency
        self.build_slider_group("Basic Adjustments", [
            ('gamma', 'Gamma', 0.1, 3.0, 0.01),
            ('exposure', 'Exposure', -2.0, 2.0, 0.01),
            ('brightness', 'Brightness', -0.5, 0.5, 0.01),
            ('contrast', 'Contrast', 0.0, 2.0, 0.01),
        ])
        
        self.build_slider_group("Levels", [
            ('blacks', 'Blacks (Lift)', 0.0, 0.3, 0.01),
            ('whites', 'Whites (Clip)', 0.7, 1.0, 0.01),
            ('fade', 'Fade', 0.0, 0.5, 0.01),
        ])
        
        self.build_slider_group("Color", [
            ('saturation', 'Saturation', 0.0, 2.0, 0.01),
            ('vibrance', 'Vibrance', -1.0, 1.0, 0.01),
            ('temperature', 'Temperature', -0.3, 0.3, 0.01),
            ('hue_shift', 'Hue Shift', 0.0, 1.0, 0.01),
        ])
        
        self.build_slider_group("Global Tint (RGB)", [
            ('tint_r', 'Red', 0.0, 2.0, 0.01),
            ('tint_g', 'Green', 0.0, 2.0, 0.01),
            ('tint_b', 'Blue', 0.0, 2.0, 0.01),
        ])
        
        self.build_slider_group("Shadow Tint", [
            ('shadow_r', 'Red', 0.0, 2.0, 0.01),
            ('shadow_g', 'Green', 0.0, 2.0, 0.01),
            ('shadow_b', 'Blue', 0.0, 2.0, 0.01),
        ])
        
        self.build_slider_group("Midtone Tint", [
            ('midtone_r', 'Red', 0.0, 2.0, 0.01),
            ('midtone_g', 'Green', 0.0, 2.0, 0.01),
            ('midtone_b', 'Blue', 0.0, 2.0, 0.01),
        ])
        
        self.build_slider_group("Highlight Tint", [
            ('highlight_r', 'Red', 0.0, 2.0, 0.01),
            ('highlight_g', 'Green', 0.0, 2.0, 0.01),
            ('highlight_b', 'Blue', 0.0, 2.0, 0.01),
        ])
        
        self.build_slider_group("Effects", [
            ('rainbow', 'Rainbow', 0.0, 1.0, 0.01),
        ])
        
        # Invert checkbox
        invert_frame = tk.Frame(self.params_frame, bg=self.colors['bg_medium'])
        invert_frame.pack(fill='x', padx=10, pady=5)
        
        invert_cb = tk.Checkbutton(invert_frame, text="Invert Colors",
                                   variable=self.params['invert'],
                                   bg=self.colors['bg_medium'],
                                   fg=self.colors['text'],
                                   selectcolor=self.colors['bg_light'],
                                   activebackground=self.colors['bg_medium'],
                                   activeforeground=self.colors['text'],
                                   font=('Segoe UI', 10),
                                   command=self.schedule_preview_update)
        invert_cb.pack(anchor='w')
        
        # Force update to set scroll region
        self.params_frame.update_idletasks()
        self.param_canvas.configure(scrollregion=self.param_canvas.bbox('all'))
        
    def build_slider_group(self, title, sliders):
        """Build a group of sliders with a title."""
        group_frame = tk.Frame(self.params_frame, bg=self.colors['bg_medium'])
        group_frame.pack(fill='x', padx=5, pady=(10, 5))
        
        title_label = tk.Label(group_frame, text=title, 
                              bg=self.colors['bg_medium'],
                              fg=self.colors['text'],
                              font=('Segoe UI', 11, 'bold'))
        title_label.pack(anchor='w', padx=5, pady=(0, 5))
        
        for param_key, label, min_val, max_val, resolution in sliders:
            self.build_slider(group_frame, param_key, label, min_val, max_val, resolution)
    
    def build_slider(self, parent, param_key, label, min_val, max_val, resolution):
        """Build a single slider control with clickable value."""
        frame = tk.Frame(parent, bg=self.colors['bg_medium'])
        frame.pack(fill='x', padx=5, pady=3)
        
        # Label and value display
        label_frame = tk.Frame(frame, bg=self.colors['bg_medium'])
        label_frame.pack(fill='x')
        
        name_label = tk.Label(label_frame, text=label, 
                             bg=self.colors['bg_medium'],
                             fg=self.colors['text'],
                             font=('Segoe UI', 10))
        name_label.pack(side='left')
        
        # Container for value label/entry
        value_container = tk.Frame(label_frame, bg=self.colors['bg_medium'])
        value_container.pack(side='right')
        
        # Value label (clickable)
        value_label = tk.Label(value_container, text=f"{self.params[param_key].get():.2f}", 
                              bg=self.colors['bg_medium'],
                              fg=self.colors['accent'],
                              font=('Segoe UI', 9, 'bold'),
                              cursor='hand2')
        value_label.pack()
        
        # Bind click to show entry
        def show_entry(event):
            value_label.pack_forget()
            entry = tk.Entry(value_container, width=6,
                           bg=self.colors['bg_light'], fg=self.colors['text'],
                           insertbackground=self.colors['text'],
                           font=('Segoe UI', 9), relief='flat',
                           justify='center')
            entry.insert(0, f"{self.params[param_key].get():.2f}")
            entry.pack()
            entry.focus_set()
            entry.select_range(0, 'end')
            
            def apply_value(event=None):
                try:
                    new_val = float(entry.get())
                    new_val = max(min_val, min(max_val, new_val))  # Clamp to range
                    self.params[param_key].set(new_val)
                    value_label.configure(text=f"{new_val:.2f}")
                    self.schedule_preview_update()
                except ValueError:
                    pass
                entry.destroy()
                value_label.pack()
            
            def cancel_entry(event=None):
                entry.destroy()
                value_label.pack()
            
            entry.bind('<Return>', apply_value)
            entry.bind('<Escape>', cancel_entry)
            entry.bind('<FocusOut>', apply_value)
        
        value_label.bind('<Button-1>', show_entry)
        
        # Slider
        slider = tk.Scale(frame, from_=min_val, to=max_val,
                         orient='horizontal',
                         variable=self.params[param_key],
                         resolution=resolution,
                         showvalue=False,
                         bg=self.colors['bg_medium'],
                         fg=self.colors['text'],
                         troughcolor=self.colors['slider_trough'],
                         activebackground=self.colors['slider_active'],
                         highlightthickness=0,
                         length=300,
                         command=lambda v, vl=value_label, pk=param_key: self.on_slider_change(v, vl, pk))
        slider.pack(fill='x')
        
    def on_slider_change(self, value, value_label, param_key):
        """Handle slider value change."""
        value_label.configure(text=f"{float(value):.2f}")
        self.schedule_preview_update()
        
    def schedule_preview_update(self):
        """Schedule a preview update with debouncing."""
        if not self.update_scheduled:
            self.update_scheduled = True
            self.root.after(300, self.update_preview)  # 300ms debounce for smoother sliders
    
    def build_action_buttons(self, parent):
        """Build action buttons with modern styling."""
        btn_frame = tk.Frame(parent, bg=self.colors['bg_card'],
                            highlightbackground=self.colors['border'],
                            highlightthickness=1)
        btn_frame.pack(fill='x', pady=0)
        
        inner_frame = tk.Frame(btn_frame, bg=self.colors['bg_card'])
        inner_frame.pack(fill='x', padx=12, pady=12)
        
        # Top row - utility buttons
        top_row = tk.Frame(inner_frame, bg=self.colors['bg_card'])
        top_row.pack(fill='x', pady=(0, 8))
        
        # Reset button
        reset_btn = tk.Button(top_row, text="↻ Reset All",
                             bg=self.colors['bg_light'], fg=self.colors['text'],
                             font=('Segoe UI', 9), relief='flat', cursor='hand2',
                             activebackground=self.colors['bg_medium'],
                             activeforeground=self.colors['text'],
                             command=self.reset_params)
        reset_btn.pack(side='left', ipadx=15, ipady=6)
        
        # Load preview image button
        preview_btn = tk.Button(top_row, text="📷 Load Image",
                               bg=self.colors['bg_light'], fg=self.colors['text'],
                               font=('Segoe UI', 9), relief='flat', cursor='hand2',
                               activebackground=self.colors['bg_medium'],
                               activeforeground=self.colors['text'],
                               command=self.browse_preview_image)
        preview_btn.pack(side='left', padx=(8, 0), ipadx=15, ipady=6)
        
        # Export button - full width accent color
        export_btn = tk.Button(inner_frame, text="Export LUT",
                              bg=self.colors['accent'], fg='white',
                              font=('Segoe UI', 11, 'bold'), relief='flat', cursor='hand2',
                              activebackground=self.colors['accent_hover'],
                              activeforeground='white',
                              command=self.process_and_export)
        export_btn.pack(fill='x', ipady=10)
        
        # Pack to VPK button - green/secondary accent
        pack_btn = tk.Button(inner_frame, text="📦 Pack to VPK",
                            bg=self.colors['accent_secondary'], fg='white',
                            font=('Segoe UI', 10, 'bold'), relief='flat', cursor='hand2',
                            activebackground='#16a34a',
                            activeforeground='white',
                            command=self.pack_to_vpk)
        pack_btn.pack(fill='x', ipady=8, pady=(8, 0))
        
    def build_preview_section(self, parent):
        """Build the preview panel with images filling available space."""
        # Compact header
        header_frame = tk.Frame(parent, bg=self.colors['bg_card'])
        header_frame.pack(fill='x', padx=12, pady=(8, 4))
        
        preview_title = tk.Label(header_frame, text="Preview",
                                bg=self.colors['bg_card'],
                                fg=self.colors['text'],
                                font=('Segoe UI', 11, 'bold'))
        preview_title.pack(side='left')
        
        self.status_label = tk.Label(header_frame, text="LUT: 32³ (32768 entries)",
                                    bg=self.colors['bg_card'],
                                    fg=self.colors['text_dim'],
                                    font=('Segoe UI', 9))
        self.status_label.pack(side='right')
        
        # Preview container - uses all remaining space
        preview_container = tk.Frame(parent, bg=self.colors['bg_dark'])
        preview_container.pack(fill='both', expand=True, padx=8, pady=(0, 8))
        
        # Use grid layout for even distribution
        preview_container.columnconfigure(0, weight=1)
        preview_container.columnconfigure(1, weight=1)
        preview_container.rowconfigure(0, weight=0)  # Labels
        preview_container.rowconfigure(1, weight=1)  # Canvases
        
        # Before column
        before_title = tk.Label(preview_container, text="Original",
                               bg=self.colors['bg_dark'],
                               fg=self.colors['text_dim'],
                               font=('Segoe UI', 9))
        before_title.grid(row=0, column=0, pady=(0, 4))
        
        self.before_canvas = tk.Canvas(preview_container, bg=self.colors['bg_dark'],
                                       highlightthickness=0)
        self.before_canvas.grid(row=1, column=0, sticky='nsew', padx=(0, 4))
        
        # After column
        after_title = tk.Label(preview_container, text="With LUT Applied",
                              bg=self.colors['bg_dark'],
                              fg=self.colors['text_dim'],
                              font=('Segoe UI', 9))
        after_title.grid(row=0, column=1, pady=(0, 4))
        
        self.after_canvas = tk.Canvas(preview_container, bg=self.colors['bg_dark'],
                                      highlightthickness=0)
        self.after_canvas.grid(row=1, column=1, sticky='nsew', padx=(4, 0))
        
        # Bind resize events
        self.before_canvas.bind('<Configure>', lambda e: self.on_canvas_resize())
        
    def browse_input(self):
        """Open file dialog for input LUT file."""
        filepath = filedialog.askopenfilename(
            title="Select LUT File",
            filetypes=[("LUT files", "*.vpost *.raw"), ("VPOST files", "*.vpost"), ("RAW files", "*.raw"), ("All files", "*.*")]
        )
        if filepath:
            self.input_path.set(filepath)
            self.load_lut_file()
            
    def browse_output(self):
        """Open directory dialog for output."""
        dirpath = filedialog.askdirectory(title="Select Output Directory")
        if dirpath:
            self.output_dir.set(dirpath)
            
    def browse_preview_image(self):
        """Open file dialog for preview image."""
        filepath = filedialog.askopenfilename(
            title="Select Preview Image",
            filetypes=[("Image files", "*.png *.jpg *.jpeg *.bmp"), ("All files", "*.*")]
        )
        if filepath:
            self.load_preview_image(filepath)
            
    def load_preview_image(self, filepath):
        """Load and display a preview image."""
        try:
            img = Image.open(filepath)
            img = img.convert('RGB')
            self.preview_image_original = img
            self.preview_image_array = np.array(img)
            self.update_preview()
            self.status_label.configure(text=f"Loaded: {os.path.basename(filepath)}")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load image: {str(e)}")
            
    def load_lut_file(self):
        """Load and parse the input LUT file (.vpost or .raw)."""
        try:
            filepath = self.input_path.get()
            
            if filepath.lower().endswith('.raw'):
                # Load binary .raw file
                self.original_lut, self.lut_dim = parse_raw_file(filepath)
                self.header = None
                self.footer = None
                self.is_raw_file = True
            else:
                # Load .vpost file with embedded LUT
                self.header, lut_values, self.footer = parse_vpost_file(filepath)
                
                if len(lut_values) % 3 != 0:
                    raise ValueError(f"LUT data length {len(lut_values)} is not divisible by 3")
                
                self.original_lut = np.array(lut_values, dtype=np.float32).reshape(-1, 3)
                lut_size = len(self.original_lut)
                self.lut_dim = round(lut_size ** (1/3))
                self.is_raw_file = False
            
            lut_size = len(self.original_lut)
            self.status_label.configure(text=f"LUT: {self.lut_dim}³ ({lut_size} entries)")
            self.update_preview()
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load LUT file: {str(e)}")
            
    def get_current_params(self):
        """Get current parameter values as a dict."""
        return {k: v.get() for k, v in self.params.items()}
    
    def reset_params(self):
        """Reset all parameters to defaults."""
        for key, default_val in self.defaults.items():
            self.params[key].set(default_val)
        self.update_preview()
        
    def update_preview(self):
        """Update the preview with current LUT settings (non-blocking)."""
        self.update_scheduled = False
        
        if self.preview_image_array is None or self.original_lut is None:
            return
        
        # Get current parameters before starting thread
        params = self.get_current_params()
        
        def process_in_background():
            try:
                # Process LUT
                processed_lut = process_lut(self.original_lut.copy(), params)
                
                # Apply LUT to image
                result_array = apply_lut_to_image(
                    self.preview_image_array, 
                    processed_lut, 
                    self.lut_dim
                )
                
                # Convert to PIL Image
                result_img = Image.fromarray(result_array)
                
                # Schedule UI update on main thread
                self.root.after(0, lambda: self.display_preview_images(
                    self.preview_image_original, result_img))
                
            except Exception as e:
                self.root.after(0, lambda: self.status_label.configure(
                    text=f"Preview error: {str(e)}"))
        
        # Run in background thread
        thread = threading.Thread(target=process_in_background, daemon=True)
        thread.start()
            
    def display_preview_images(self, original, processed):
        """Display before/after images on canvases."""
        # Get canvas dimensions
        canvas_width = self.before_canvas.winfo_width()
        canvas_height = self.before_canvas.winfo_height()
        
        if canvas_width <= 1 or canvas_height <= 1:
            return
            
        # Resize images to fit canvas while maintaining aspect ratio
        def fit_image(img, max_w, max_h):
            ratio = min(max_w / img.width, max_h / img.height)
            new_size = (int(img.width * ratio), int(img.height * ratio))
            return img.resize(new_size, Image.Resampling.LANCZOS)
        
        orig_resized = fit_image(original, canvas_width - 10, canvas_height - 10)
        proc_resized = fit_image(processed, canvas_width - 10, canvas_height - 10)
        
        # Convert to PhotoImage
        self.before_photo = ImageTk.PhotoImage(orig_resized)
        self.after_photo = ImageTk.PhotoImage(proc_resized)
        
        # Display on canvases
        self.before_canvas.delete('all')
        self.after_canvas.delete('all')
        
        self.before_canvas.create_image(
            canvas_width // 2, canvas_height // 2,
            image=self.before_photo, anchor='center'
        )
        self.after_canvas.create_image(
            canvas_width // 2, canvas_height // 2,
            image=self.after_photo, anchor='center'
        )
        
    def on_canvas_resize(self):
        """Handle canvas resize event."""
        self.schedule_preview_update()
        
    def process_and_export(self):
        """Process the LUT and export files."""
        if self.original_lut is None:
            messagebox.showerror("Error", "No LUT file loaded!")
            return
            
        if not self.output_dir.get():
            messagebox.showerror("Error", "No output directory selected!")
            return
            
        try:
            self.status_label.configure(text="Processing...")
            self.root.update()
            
            # Get current parameters
            params = self.get_current_params()
            
            # Process LUT
            processed_lut = process_lut(self.original_lut, params)
            
            # Output path
            output_vpost = os.path.join(self.output_dir.get(), "basepostprocess_deadlock.vpost")
            
            # Build VPOST with embedded LUT data
            self.status_label.configure(text="Writing VPOST...")
            self.root.update()
            
            with open(output_vpost, 'w', encoding='utf-8') as f:
                # Write header with Tone Mapping and Bloom layers
                f.write('<!-- kv3 encoding:text:version{e21c7f3c-8a33-41c5-9977-a76d3a32aa0d} format:generic:version{7412167c-06e9-4698-aff2-e63eb59037e7} -->\n')
                f.write('{\n')
                f.write('\t_class = "CPostProcessData"\n')
                f.write('\tm_layers = \n')
                f.write('\t[\n')
                
                # Tone Mapping Layer
                f.write('\t\t{\n')
                f.write('\t\t\t_class = "CToneMappingLayer"\n')
                f.write('\t\t\tm_name = "Tone Mapping"\n')
                f.write('\t\t\tm_nOpacityPercent = 100\n')
                f.write('\t\t\tm_bVisible = true\n')
                f.write('\t\t\tm_pLayerMask = null\n')
                f.write('\t\t\tm_params = \n')
                f.write('\t\t\t{\n')
                f.write('\t\t\t\tm_flExposureBias = 0.000000\n')
                f.write('\t\t\t\tm_flShoulderStrength = 0.319000\n')
                f.write('\t\t\t\tm_flLinearStrength = 0.504700\n')
                f.write('\t\t\t\tm_flLinearAngle = 0.161900\n')
                f.write('\t\t\t\tm_flToeStrength = 0.466700\n')
                f.write('\t\t\t\tm_flToeNum = 0.000000\n')
                f.write('\t\t\t\tm_flToeDenom = 0.747500\n')
                f.write('\t\t\t\tm_flWhitePoint = 3.999600\n')
                f.write('\t\t\t\tm_flLuminanceSource = 0.000000\n')
                f.write('\t\t\t\tm_flExposureBiasShadows = 0.000000\n')
                f.write('\t\t\t\tm_flExposureBiasHighlights = 0.000000\n')
                f.write('\t\t\t\tm_flMinShadowLum = 0.000000\n')
                f.write('\t\t\t\tm_flMaxShadowLum = 0.000000\n')
                f.write('\t\t\t\tm_flMinHighlightLum = 0.000000\n')
                f.write('\t\t\t\tm_flMaxHighlightLum = 0.000000\n')
                f.write('\t\t\t}\n')
                f.write('\t\t},\n')
                
                # Bloom Layer
                f.write('\t\t{\n')
                f.write('\t\t\t_class = "CBloomLayer"\n')
                f.write('\t\t\tm_name = "Bloom"\n')
                f.write('\t\t\tm_nOpacityPercent = 100\n')
                f.write('\t\t\tm_bVisible = true\n')
                f.write('\t\t\tm_pLayerMask = null\n')
                f.write('\t\t\tm_params = \n')
                f.write('\t\t\t{\n')
                f.write('\t\t\t\tm_blendMode = "BLOOM_BLEND_SCREEN"\n')
                f.write('\t\t\t\tm_flBloomStrength = 0.000000\n')
                f.write('\t\t\t\tm_flScreenBloomStrength = 1.000000\n')
                f.write('\t\t\t\tm_flBlurBloomStrength = 0.000000\n')
                f.write('\t\t\t\tm_flBloomThreshold = 1.976000\n')
                f.write('\t\t\t\tm_flBloomThresholdWidth = 2.365500\n')
                f.write('\t\t\t\tm_flSkyboxBloomStrength = 1.000000\n')
                f.write('\t\t\t\tm_flBloomStartValue = 1.000000\n')
                f.write('\t\t\t\tm_flBlurWeight = \n')
                f.write('\t\t\t\t[\n')
                for _ in range(5):
                    f.write('\t\t\t\t\t0.200000,\n')
                f.write('\t\t\t\t]\n')
                f.write('\t\t\t\tm_vBlurTint = \n')
                f.write('\t\t\t\t[\n')
                for _ in range(5):
                    f.write('\t\t\t\t\t\n')
                    f.write('\t\t\t\t\t[\n')
                    f.write('\t\t\t\t\t\t1.000000,\n')
                    f.write('\t\t\t\t\t\t1.000000,\n')
                    f.write('\t\t\t\t\t\t1.000000,\n')
                    f.write('\t\t\t\t\t],\n')
                f.write('\t\t\t\t]\n')
                f.write('\t\t\t}\n')
                f.write('\t\t},\n')
                
                # Color Lookup Table Layer with embedded LUT
                f.write('\t\t{\n')
                f.write('\t\t\t_class = "CColorLookupColorCorrectionLayer"\n')
                f.write('\t\t\tm_name = "VRF Extracted Lookup Table"\n')
                f.write('\t\t\tm_nOpacityPercent = 100\n')
                f.write('\t\t\tm_bVisible = true\n')
                f.write('\t\t\tm_pLayerMask = null\n')
                f.write('\t\t\tm_fileName = "postprocessing/basepostprocess.raw"\n')
                f.write('\t\t\tm_lut = \n')
                f.write('\t\t\t[\n')
                
                # Write all LUT values (R, G, B for each entry)
                for r, g, b in processed_lut:
                    f.write(f'\t\t\t\t{r:.6f},\n')
                    f.write(f'\t\t\t\t{g:.6f},\n')
                    f.write(f'\t\t\t\t{b:.6f},\n')
                
                # Close LUT array and layer
                f.write('\t\t\t]\n')
                f.write('\t\t\tm_nDim = 32\n')
                f.write('\t\t},\n')
                
                # Close layers array and root
                f.write('\t]\n')
                f.write('}\n')
            
            self.status_label.configure(text="Export complete!")
            
        except Exception as e:
            self.status_label.configure(text="Export failed!")
            messagebox.showerror("Error", f"Export failed: {str(e)}")
    
    def pack_to_vpk(self):
        """Pack the exported vpost to VPK using native Python (no DeadPacker.exe required)."""
        # Check vpk library availability
        if not VPK_AVAILABLE:
            messagebox.showerror("Error", 
                "VPK library not installed!\n\n"
                "Please run: pip install vpk\n\n"
                "Then restart the application.")
            return
        
        # Auto-export first
        if self.original_lut is None:
            messagebox.showerror("Error", "No LUT file loaded!")
            return
        
        # Export the LUT first
        self.process_and_export()
        
        # Check if export succeeded
        output_vpost = os.path.join(self.output_dir.get(), "basepostprocess_deadlock.vpost")
        if not os.path.exists(output_vpost):
            messagebox.showerror("Error", "Export failed! Cannot pack to VPK.")
            return
        
        # No DeadPacker.exe needed - using native Python vpk library
        # Create configuration dialog
        config_window = tk.Toplevel(self.root)
        config_window.title("Pack to VPK Configuration")
        config_window.geometry("600x400")
        config_window.configure(bg=self.colors['bg_dark'])
        config_window.transient(self.root)
        config_window.grab_set()
        
        # Variables
        detected_csdk = find_csdk_path(self.app_dir)
        detected_deadlock = find_deadlock_path()
        default_vpk_output = (
            os.path.join(detected_deadlock, "game", "citadel", "addons", "pak02_dir.vpk")
            if detected_deadlock else ""
        )

        csdk_path = tk.StringVar(value=detected_csdk)
        addon_name = tk.StringVar(value="random post process")
        vpk_output = tk.StringVar(value=default_vpk_output)
        
        # Title
        title_label = tk.Label(config_window, text="VPK Packing Configuration",
                              bg=self.colors['bg_dark'], fg=self.colors['text'],
                              font=('Segoe UI', 14, 'bold'))
        title_label.pack(pady=(15, 20))
        
        # Form frame
        form_frame = tk.Frame(config_window, bg=self.colors['bg_dark'])
        form_frame.pack(fill='x', padx=20)
        
        # CSDK Path
        tk.Label(form_frame, text="CSDK/SDK Path:", bg=self.colors['bg_dark'], 
                fg=self.colors['text_label'], font=('Segoe UI', 9)).pack(anchor='w')
        csdk_row = tk.Frame(form_frame, bg=self.colors['bg_dark'])
        csdk_row.pack(fill='x', pady=(2, 10))
        tk.Entry(csdk_row, textvariable=csdk_path, bg=self.colors['bg_light'],
                fg=self.colors['text'], font=('Segoe UI', 9), relief='flat').pack(side='left', fill='x', expand=True, ipady=5)
        tk.Button(csdk_row, text="Browse", bg=self.colors['bg_light'], fg=self.colors['text'],
                 font=('Segoe UI', 9), relief='flat',
                 command=lambda: csdk_path.set(filedialog.askdirectory() or csdk_path.get())).pack(side='right', padx=(5, 0), ipadx=10, ipady=3)
        
        # Addon Name
        tk.Label(form_frame, text="Addon Name:", bg=self.colors['bg_dark'],
                fg=self.colors['text_label'], font=('Segoe UI', 9)).pack(anchor='w')
        tk.Entry(form_frame, textvariable=addon_name, bg=self.colors['bg_light'],
                fg=self.colors['text'], font=('Segoe UI', 9), relief='flat').pack(fill='x', ipady=5, pady=(2, 10))
        
        # VPK Output Path
        tk.Label(form_frame, text="VPK Output Path:", bg=self.colors['bg_dark'],
                fg=self.colors['text_label'], font=('Segoe UI', 9)).pack(anchor='w')
        vpk_row = tk.Frame(form_frame, bg=self.colors['bg_dark'])
        vpk_row.pack(fill='x', pady=(2, 10))
        tk.Entry(vpk_row, textvariable=vpk_output, bg=self.colors['bg_light'],
                fg=self.colors['text'], font=('Segoe UI', 9), relief='flat').pack(side='left', fill='x', expand=True, ipady=5)
        tk.Button(vpk_row, text="Browse", bg=self.colors['bg_light'], fg=self.colors['text'],
                 font=('Segoe UI', 9), relief='flat',
                 command=lambda: vpk_output.set(filedialog.asksaveasfilename(defaultextension=".vpk", filetypes=[("VPK files", "*.vpk")]) or vpk_output.get())).pack(side='right', padx=(5, 0), ipadx=10, ipady=3)
        
        def run_packer():
            config_window.destroy()
            self.execute_pack_workflow(csdk_path.get(), addon_name.get(), vpk_output.get())
        
        # Buttons
        btn_frame = tk.Frame(config_window, bg=self.colors['bg_dark'])
        btn_frame.pack(fill='x', padx=20, pady=20)
        
        tk.Button(btn_frame, text="Cancel", bg=self.colors['bg_light'], fg=self.colors['text'],
                 font=('Segoe UI', 10), relief='flat', command=config_window.destroy).pack(side='left', ipadx=20, ipady=6)
        
        tk.Button(btn_frame, text="Pack VPK", bg=self.colors['accent_secondary'], fg='white',
                 font=('Segoe UI', 10, 'bold'), relief='flat', command=run_packer).pack(side='right', ipadx=20, ipady=6)
    
    def execute_pack_workflow(self, csdk_path, addon_name, vpk_output):
        """Execute the full packing workflow: copy, compile, close Deadlock, pack VPK."""
        try:
            if not csdk_path:
                messagebox.showerror(
                    "CSDK Required",
                    "VPK packing requires a local Deadlock-compatible Source 2 toolchain.\n\n"
                    "Select its root folder or set DEADLOCK_CSDK_PATH."
                )
                return
            if not addon_name.strip():
                messagebox.showerror("Error", "Enter an addon name.")
                return
            if not vpk_output:
                messagebox.showerror("Error", "Choose a VPK output path.")
                return

            output_dir = self.output_dir.get()
            
            # === STEP 1: Copy files to CSDK content directory ===
            self.status_label.configure(text="Step 1/4: Copying files...")
            self.root.update()
            
            # Create postprocessing folder in output directory if it doesn't exist
            postprocessing_dir = os.path.join(output_dir, "postprocessing")
            os.makedirs(postprocessing_dir, exist_ok=True)
            
            # Copy vpost to postprocessing subfolder
            vpost_src = os.path.join(output_dir, "basepostprocess_deadlock.vpost")
            vpost_dest = os.path.join(postprocessing_dir, "basepostprocess_deadlock.vpost")
            if os.path.exists(vpost_src):
                shutil.copy2(vpost_src, vpost_dest)
            
            # Copy ONLY the postprocessing folder to CSDK content directory
            content_dest = os.path.join(csdk_path, "content", "citadel_addons", addon_name)
            content_postproc = os.path.join(content_dest, "postprocessing")
            
            # Clean and recreate destination
            if os.path.exists(content_dest):
                shutil.rmtree(content_dest)
            os.makedirs(content_dest, exist_ok=True)
            
            # Copy only the postprocessing folder
            shutil.copytree(postprocessing_dir, content_postproc)
            
            # === STEP 2: Compile with resourcecompiler.exe ===
            self.status_label.configure(text="Step 2/4: Compiling resources...")
            self.root.update()
            
            resource_compiler = os.path.join(csdk_path, "game", "bin_tools", "win64", "resourcecompiler.exe")
            if not os.path.exists(resource_compiler):
                messagebox.showerror("Error", f"Resource compiler not found:\n{resource_compiler}")
                return
            
            compile_args = [
                resource_compiler,
                "-r",
                "-i", f"{content_dest.rstrip('/\\\\')}\\**",
                "-danger_mode_ignore_schema_mismatches"
            ]
            
            result = subprocess.run(compile_args, capture_output=True, text=True)
            if result.returncode != 0:
                messagebox.showerror("Compile Error", 
                    f"Resource compiler failed!\n\n"
                    f"Output: {result.stdout[:500]}\n"
                    f"Error: {result.stderr[:500]}")
                return
            
            # === STEP 3: Close Deadlock if running ===
            self.status_label.configure(text="Step 3/4: Closing Deadlock...")
            self.root.update()
            
            try:
                # Find and kill Deadlock process
                if sys.platform == 'win32':
                    subprocess.run(['taskkill', '/F', '/IM', 'project8.exe'], 
                                  capture_output=True, check=False)
                    subprocess.run(['taskkill', '/F', '/IM', 'deadlock.exe'], 
                                  capture_output=True, check=False)
            except Exception:
                pass  # Game might not be running
            
            import time
            time.sleep(0.5)  # Brief delay to allow file locks to release
            
            # === STEP 4: Pack VPK ===
            self.status_label.configure(text="Step 4/4: Packing VPK...")
            self.root.update()
            
            # Find the compiled output directory
            # NOTE: resourcecompiler lowercases the directory name
            compiled_dir = os.path.join(csdk_path, "game", "citadel_addons", addon_name.lower())
            
            # Debug: If directory doesn't exist, show what's actually in the citadel_addons folder
            citadel_addons_dir = os.path.join(csdk_path, "game", "citadel_addons")
            if not os.path.exists(compiled_dir):
                if os.path.exists(citadel_addons_dir):
                    existing_dirs = os.listdir(citadel_addons_dir)
                    messagebox.showerror("Error", 
                        f"Compiled directory not found:\n{compiled_dir}\n\n"
                        f"Available in citadel_addons:\n{existing_dirs}")
                else:
                    messagebox.showerror("Error", 
                        f"Compiled directory not found:\n{compiled_dir}\n\n"
                        f"citadel_addons folder doesn't exist:\n{citadel_addons_dir}")
                return
            
            # Exclusion patterns - remove excluded files before packing
            exclude_patterns = ["cache_*.soc", "tools_thumbnail_cache.bin"]
            
            # Remove excluded files from compiled directory before packing
            for root_dir, dirs, files in os.walk(compiled_dir):
                for filename in files:
                    for pattern in exclude_patterns:
                        if fnmatch.fnmatch(filename, pattern):
                            filepath = os.path.join(root_dir, filename)
                            try:
                                os.remove(filepath)
                            except:
                                pass
            
            # Count files for verification
            file_count = sum(len(files) for _, _, files in os.walk(compiled_dir))
            
            if file_count == 0:
                # Debug: Show directory info
                messagebox.showerror("Error", 
                    f"No files to pack!\n\n"
                    f"Compiled dir: {compiled_dir}")
                return
            
            # Create VPK from directory using correct API
            new_vpk = vpk.new(compiled_dir)
            new_vpk.save(vpk_output)
            
            self.status_label.configure(text=f"VPK packed! ({file_count} files)")
                
        except Exception as e:
            self.status_label.configure(text="Packing error!")
            messagebox.showerror("Error", f"Failed to pack VPK: {str(e)}")


def main():
    root = tk.Tk()
    app = LUTProcessorGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
