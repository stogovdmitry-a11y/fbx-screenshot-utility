# FBX Screenshot Utility

Desktop app for Windows. Drop an FBX in, assign textures to the existing material slots, and build a turnaround sheet. Blender 4.5 renders in the background. There is no live viewport.

![Screenshot Utility](docs/screenshot.png)

## What it does

- Reads an FBX and keeps the material slot names. Head and hair stay separate.
- An empty texture field keeps whatever is already stored in the FBX.
- Builds one PNG sheet. Each row is a pass, each column is a camera angle.
- The **Long side** value is the long side of the finished sheet, not of a single frame.
- Camera: frame count, even step, start angle, distance scale, and height as a fraction of the model. `0°` looks from the chosen front (`−Y`, `+Y`, `−X`, `+X`). Up is Z.
- Optional rows: Base Color (unlit), Normal, and a lit Material. Wireframe can be added as its own row, in a chosen color, over a chosen pass.
- Name and task are printed on the sheet. The date can be added to the footer and to the file name. If that file already exists, a number is appended.
- Background color is shared by the render and the sheet padding.
- The sheet can open in its own window. Last folders, texture assignments, and options are remembered.

## Character Pipeline

On by default. This is the packed character layout, not a set of separate roughness and metallic files.

| Slot | What it is |
| --- | --- |
| Base Color | Albedo. Opacity can live in the alpha channel. |
| Masks | Packed mask. See the channels below. |
| Normal | Normal map. |
| Special Masks | Optional extra mask. Off unless you turn that row on. |

**Masks**

| Channel | Map |
| --- | --- |
| R | Roughness. Bright is rough. |
| G | Emissive. |
| B | Metallic. |
| A | Translucency. |

**Alpha from Base Color** is off by default. Turn it on only when the base color really carries opacity. Hair cards often need it. A solid head usually does not.

**Special Masks** is an optional texture and an optional sheet row. Leave both alone unless you want that mask on the sheet.

Turn Character Pipeline off to assign Roughness or Smoothness, Metallic, AO, and Opacity as separate files. Smoothness means bright is glossy (`1 − roughness`).

## Run

Windows. Two programs have to be installed first:

- [Python 3.13](https://www.python.org/downloads/windows/). Use the official Windows installer and keep the **py launcher** enabled. The `py -3.13` command below comes from that installer.
- [Blender 4.5](https://www.blender.org/download/releases/4-5/). The sheet is rendered by Blender in the background. There is no live viewport.

Then, from this folder:

```bat
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

That installs the window toolkit and the image library. Launch `Screenshot Utility.bat` after that. If `blender.exe` is not in `C:\Program Files\Blender Foundation\Blender 4.5\`, set the path at the bottom of the window.

## Blender addon

`addons/screenshot_utility.zip` is a Blender 4.5 addon. In Blender: Edit → Preferences → Add-ons → Install from Disk, pick that zip, then enable **Screenshot Utility**.

The button is **Build Screen List**. It sits in the top right corner of the Blender window and has a monitor icon.

It exports the meshes and starts a sheet in this utility, using the settings already set here (angles, passes, background, captions).

- A selection sends those meshes and their child meshes. With nothing selected, it sends every visible mesh.
- Textures already on the materials stay in the FBX. Empty texture fields in the utility still keep whatever the file already has.
- If this window is closed, the button opens it and then renders. Open the utility once so the addon knows where it lives. If it still cannot find it, set **Utility folder** in the addon preferences to this project folder.
