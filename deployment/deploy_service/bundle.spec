# -*- mode: python -*-


import glob
import os
import os.path
import pathlib
import platform
import sys

from PyInstaller.utils.hooks import collect_all

dependencies = [
    "av",
    "pyglui",
    "pupil_apriltags",
    "sklearn",
    "pye3d",
    "glfw",
    "pupil_apriltags",
    "numpy",
    "scipy",
]
if platform.system() != "Windows":
    dependencies.append("cysignals")


def sum_lists(lists_to_sum):
    return sum(lists_to_sum, [])


module_collection = [collect_all(dep) for dep in dependencies]
datas, binaries, hidden_imports = map(sum_lists, zip(*module_collection))


if platform.system() == "Darwin":
    sys.path.append(".")
    from version import pupil_version

    del sys.path[-1]

    a = Analysis(
        ["../../pupil_src/main.py"],
        pathex=["../../pupil_src/shared_modules/"],
        hiddenimports=hidden_imports,
        hookspath=None,
        runtime_hooks=["../find_opengl_bigsur.py"],
        excludes=["matplotlib"],
        datas=datas,
        binaries=binaries,
    )
    pyz = PYZ(a.pure)
    exe = EXE(
        pyz,
        a.scripts,
        exclude_binaries=True,
        name="pupil_service",
        debug=False,
        strip=None,
        upx=False,
        console=True,
        target_arch="x86_64",
        codesign_identity="Developer ID Application: Pupil Labs UG (haftungsbeschrankt) (R55K9ESN6B)",
        entitlements_file="../entitlements.plist",
    )

    # exclude system lib.
    libSystem = [bn for bn in a.binaries if "libSystem.dylib" in bn]
    coll = COLLECT(
        exe,
        a.binaries - libSystem,
        a.zipfiles,
        a.datas,
        strip=None,
        upx=True,
        name="Pupil Service",
    )

    app = BUNDLE(
        coll,
        name="Pupil Service.app",
        icon="pupil-service.icns",
        bundle_identifier="com.pupil-labs.core.service",
        version=str(pupil_version()),
        info_plist={"NSHighResolutionCapable": "True"},
    )


elif platform.system() == "Linux":
    a = Analysis(
        ["../../pupil_src/main.py"],
        pathex=["../../pupil_src/shared_modules/"],
        hiddenimports=hidden_imports,
        hookspath=None,
        runtime_hooks=None,
        excludes=["matplotlib"],
        datas=datas,
        binaries=binaries,
    )

    pyz = PYZ(a.pure)
    exe = EXE(
        pyz,
        a.scripts,
        exclude_binaries=True,
        name="pupil_service",
        debug=False,
        strip=False,
        upx=True,
        console=True,
    )

    # libc is also not meant to travel with the bundle. Otherwise pyre.helpers with segfault.
    binaries = [b for b in a.binaries if not "libc.so" in b[0]]

    # libstdc++ is also not meant to travel with the bundle. Otherwise nvideo opengl drivers will fail to load.
    binaries = [b for b in binaries if not "libstdc++.so" in b[0]]

    # required for 14.04 16.04 interoperability.
    binaries = [b for b in binaries if not "libgomp.so.1" in b[0]]

    # required for 17.10 interoperability.
    binaries = [b for b in binaries if not "libdrm.so.2" in b[0]]

    coll = COLLECT(
        exe,
        binaries,
        a.zipfiles,
        a.datas,
        strip=True,
        upx=True,
        name="pupil_service",
    )

elif platform.system() == "Windows":
    import os
    import os.path
    import sys

    external_libs_path = pathlib.Path("../../pupil_external")

    a = Analysis(
        ["../../pupil_src/main.py"],
        pathex=["../../pupil_src/shared_modules/", str(external_libs_path)],
        binaries=binaries,
        datas=datas,
        hiddenimports=hidden_imports,
        hookspath=None,
        runtime_hooks=None,
        win_no_prefer_redirects=False,
        win_private_assemblies=False,
        excludes=["matplotlib"],
    )

    pyz = PYZ(a.pure)
    exe = EXE(
        pyz,
        a.scripts,
        exclude_binaries=True,
        name="pupil_service.exe",
        icon="pupil-service.ico",
        debug=False,
        strip=None,
        upx=True,
        console=False,
        resources=["pupil-service.ico,ICON"],
    )

    vc_redist_path = external_libs_path / "vc_redist"
    vc_redist_libs = [
        (lib.name, str(lib), "BINARY") for lib in vc_redist_path.glob("*.dll")
    ]

    coll = COLLECT(
        exe,
        a.binaries,
        a.zipfiles,
        a.datas,
        [("PupilDrvInst.exe", "../../pupil_external/PupilDrvInst.exe", "BINARY")],
        vc_redist_libs,
        strip=False,
        upx=True,
        name="Pupil Service",
    )
