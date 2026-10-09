# Deadlock Post Processing Editor

A Windows editor for creating and previewing Deadlock post-processing LUTs. Adjust exposure, gamma, contrast, colour, levels, tints, and other grading controls with a live before/after preview, then export a Source 2 `.vpost` file.

This is an unofficial community project and is not affiliated with or endorsed by Valve Corporation.

## Quick start

1. Clone or download this repository.
2. Run `LUT Processor.exe` or double-click `run.bat`.
3. Adjust the grading controls and select **Process & Export**.

The standalone application includes its Python dependencies. Editing, previewing, and `.vpost` export do not require a CSDK.

## VPK packing

Compiling a `.vpost` into a game-ready Source 2 resource requires a Deadlock-compatible Source 2 toolchain. Valve's `resourcecompiler.exe` is not included: it is proprietary, is not located in the SDK's redistributable directory, and depends on hundreds of megabytes of adjacent engine libraries rather than operating as a standalone executable.

If you already have a compatible CSDK/toolchain, choose its root folder in the packing dialog. The editor checks these locations automatically:

- The `DEADLOCK_CSDK_PATH` environment variable.
- A `CSDK` or `Reduced_CSDK_12` folder beside the application.
- `F:\Reduced_CSDK_12` or `C:\Reduced_CSDK_12`.

The editor also discovers Deadlock in the Steam libraries registered on Windows. You can override that detection with `DEADLOCK_GAME_PATH`.

## Run from source

Python 3.11 or newer is recommended.

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python lut_processor_gui.py
```

## Build the Windows executable

Install Python 3.13, then run:

```powershell
.\build.ps1
```

The script creates a clean virtual environment, installs the pinned runtime and build dependencies, and writes `LUT Processor.exe` to the repository root.

## Repository layout

- `lut_processor_gui.py` — application source.
- `LUT Processor.exe` — ready-to-run Windows build.
- `Example/basepostprocess_deadlock.raw` — default 32×32×32 LUT.
- `Base LUT/No Post Processing New.png` — default preview image.
- `Outputs/` — generated files; ignored by Git.

## Notes

- Packing closes a running Deadlock process so that locked addon files can be replaced.
- The default output filename is `basepostprocess_deadlock.vpost`.
- The MIT license covers the original application source and project scripts. Deadlock, Source 2, and Valve trademarks and game content belong to their respective owners.

## License

Original project code is available under the [MIT License](LICENSE). See [third-party notices](THIRD_PARTY_NOTICES.md) for bundled dependencies.
