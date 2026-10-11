
from pcbc.review import render_html


def test_review_html_has_schematic_layout_3d_tabs():
    page = render_html(
        title="blinky",
        board_mm=(40.0, 25.0),
        layers=2,
        stackup="jlcpcb_2l_1oz",
        pcb_name="layout.kicad_pcb",
        source_text="Board(width=40, height=25)\n",
        sch_svg='<svg xmlns="http://www.w3.org/2000/svg"></svg>',
        front_svg="<svg></svg>",
        back_svg=None,
        copper_svg="<svg></svg>",
        silk_svg=None,
        glb_b64="AAAA",
        bom_rows=[["Comment", "Designator"], ["1k", "R1"]],
        notes=["ok"],
    )
    assert 'data-tab="sch"' in page
    assert 'data-tab="front"' in page
    assert 'data-tab="three"' in page
    assert 'id="board-3d"' in page and "window.PCBCThree" in page
    assert "Board(width=40, height=25)" in page
    assert "default.net" not in page
    assert "pcb apply schematic" not in page


def test_the_review_page_inlines_a_pinned_three_js_and_its_3d_tab_has_no_request_in_it():
    """The 3D tab did not work in Claude's in-app viewer (first a CDN script and a `data:` GLB, then model-viewer's
    fetch(Request) through the host's fetch). The page inlines the three.js subset built by tools/build_three_bundle.mjs
    (its output sha256 pinned there), keeps the GLB as base64 text, and its viewer parses those bytes: nothing in the
    viewer can make a request, and it never touches window.fetch."""
    import hashlib
    import re as _re

    from pcbc.review import GLB_VIEWER, THREE_BUNDLE

    page = render_html(
        title="b", board_mm=(10.0, 10.0), layers=2, stackup="jlcpcb_2l_1oz", pcb_name="layout.kicad_pcb", source_text=None,
        sch_svg=None, front_svg=None, back_svg=None, copper_svg=None, silk_svg=None, glb_b64="Z2xURg==", bom_rows=[], notes=[],
    )
    assert not _re.search(r"<(script|link|img)[^>]+(src|href)=\"(https?:|//)", page), "nothing is loaded from the network"
    assert "googleapis" not in page and "data:model/gltf-binary" not in page and "model-viewer" not in page
    assert "/*! three.js 0.179.1 (MIT" in page and "var PCBCThree=" in page
    assert '<script type="application/octet-stream" id="board-glb">Z2xURg==</script>' in page
    assert 'new T.GLTFLoader().parse(data, "", shown, fail);' in GLB_VIEWER
    for word in ("fetch", "XMLHttpRequest", "import(", "Worker", ".load(", "createObjectURL", "src =", "Request"):
        assert word not in GLB_VIEWER, f"the viewer has {word!r}"
    assert page.count("</script>") == page.count("<script"), "the inlined bundle closes no script early"
    vendor = THREE_BUNDLE.parent
    assert THREE_BUNDLE.name == "three-0.179.1-viewer.min.js" and (vendor / "three-0.179.1.LICENSE").exists()
    assert "MIT License" in (vendor / "three-0.179.1.LICENSE").read_text()
    assert not list(vendor.glob("model-viewer*")), "model-viewer is gone"
    build = (_Path(__file__).resolve().parents[1] / "tools" / "build_three_bundle.mjs").read_text()
    pinned = _re.search(r'output: "([0-9a-f]{64})"', build).group(1)
    assert hashlib.sha256(THREE_BUNDLE.read_bytes()).hexdigest() == pinned, "the bundle is what the build script pins"
    bare = render_html(
        title="b", board_mm=(10.0, 10.0), layers=2, stackup="jlcpcb_2l_1oz", pcb_name="layout.kicad_pcb", source_text=None,
        sch_svg=None, front_svg=None, back_svg=None, copper_svg=None, silk_svg=None, glb_b64=None, bom_rows=[], notes=[],
    )
    assert "PCBCThree" not in bare, "no model, no 600 kB viewer"


# --- The 3D tab inside a host that replaces fetch (owner's report, 2026-10-08) -------------------------------------
# Claude's desktop file pane showed the 3D tab as "Error: DataCloneError: Failed to execute 'postMessage' on 'Window':
# Request object could not be cloned.": the host swaps the page's fetch for one that posts its arguments to the parent,
# and model-viewer fetched `new Request(url)`. Answering that fetch inside the page held only while the host ASSIGNED
# its fetch; the host's code is not ours to read, so the page now makes no request at all. tools/review_host_harness.mjs
# models the host (sandboxed srcdoc frame, CSP with connect-src 'none', a postMessage fetch installed every way we could
# think of: assigned early/late/on load, re-installed, a Proxy, a getter, frozen, re-defined) and also opens the page as
# file:// and from http://localhost, and checks pixels, console, the network log and fetch's descriptor.

import json as _json
import os as _os
import re as _re
import shutil as _shutil
import struct as _struct
import subprocess as _subprocess
from pathlib import Path as _Path

import pytest as _pytest

_HARNESS = _Path(__file__).resolve().parents[1] / "tools" / "review_host_harness.mjs"
_CHROME = next((p for p in (_os.environ.get("CHROME"), "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                            "/usr/bin/google-chrome", "/usr/bin/chromium") if p and _os.path.exists(p)), None)
_HOSTS = ["file", "http", "noshim", "shim-early", "shim-late", "shim-late-fallback", "shim-late-proxy", "shim-onload",
          "shim-reinstall", "shim-getter", "shim-getter-nonconfig", "shim-frozen", "shim-define-onload", "shim-define-late",
          "getter-counts", "getter-throws"]
_OWNERS_ERROR = "DataCloneError: Failed to execute 'postMessage' on 'Window': Request object could not be cloned."


def _box_glb(w=20.0, h=1.6, d=14.0, extra=None) -> bytes:
    """A green box as a binary glTF: one mesh, no images, textures or extensions (like KiCad's board GLBs). `extra` is
    merged into the glTF JSON (to make one that names an image)."""
    x, y, z = w / 2, h / 2, d / 2
    pos = [(-x, -y, -z), (x, -y, -z), (x, y, -z), (-x, y, -z), (-x, -y, z), (x, -y, z), (x, y, z), (-x, y, z)]
    idx = [0, 2, 1, 0, 3, 2, 4, 5, 6, 4, 6, 7, 0, 1, 5, 0, 5, 4, 3, 7, 6, 3, 6, 2, 0, 4, 7, 0, 7, 3, 1, 2, 6, 1, 6, 5]
    vbin = b"".join(_struct.pack("<3f", *p) for p in pos)
    ibin = b"".join(_struct.pack("<H", i) for i in idx)
    ibin += b"\0" * (-len(ibin) % 4)
    gltf = {
        "asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1, "material": 0}]}],
        "materials": [{"pbrMetallicRoughness": {"baseColorFactor": [0.1, 0.7, 0.2, 1], "metallicFactor": 0}}],
        "buffers": [{"byteLength": len(vbin) + len(ibin)}],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(vbin)},
                        {"buffer": 0, "byteOffset": len(vbin), "byteLength": len(idx) * 2}],
        "accessors": [{"bufferView": 0, "componentType": 5126, "count": 8, "type": "VEC3",
                       "min": [-x, -y, -z], "max": [x, y, z]},
                      {"bufferView": 1, "componentType": 5123, "count": len(idx), "type": "SCALAR"}],
        **(extra or {}),
    }
    js = _json.dumps(gltf).encode()
    js += b" " * (-len(js) % 4)
    binc = vbin + ibin
    body = _struct.pack("<II", len(js), 0x4E4F534A) + js + _struct.pack("<II", len(binc), 0x004E4942) + binc
    return _struct.pack("<III", 0x46546C67, 2, 12 + len(body)) + body


def _boxes_glb(boxes) -> bytes:
    """A binary glTF of axis-aligned boxes, one mesh each: [(name, (cx, cy, cz), (w, h, d), glTF material)]. No
    normals, images or extensions (KiCad's board GLBs have none of the latter two either)."""
    idx = [0, 2, 1, 0, 3, 2, 4, 5, 6, 4, 6, 7, 0, 1, 5, 0, 5, 4, 3, 7, 6, 3, 6, 2, 0, 4, 7, 0, 7, 3, 1, 2, 6, 1, 6, 5]
    binc, views, accessors, meshes, materials = b"", [], [], [], []
    for i, (name, (cx, cy, cz), (w, h, d), material) in enumerate(boxes):
        x, y, z = w / 2, h / 2, d / 2
        corners = [(-x, -y, -z), (x, -y, -z), (x, y, -z), (-x, y, -z), (-x, -y, z), (x, -y, z), (x, y, z), (-x, y, z)]
        vbin = b"".join(_struct.pack("<3f", cx + a, cy + b, cz + c) for a, b, c in corners)
        ibin = b"".join(_struct.pack("<H", k) for k in idx) + b"\0" * (-len(idx) * 2 % 4)
        views += [{"buffer": 0, "byteOffset": len(binc), "byteLength": len(vbin)},
                  {"buffer": 0, "byteOffset": len(binc) + len(vbin), "byteLength": len(idx) * 2}]
        accessors += [{"bufferView": 2 * i, "componentType": 5126, "count": 8, "type": "VEC3",
                       "min": [cx - x, cy - y, cz - z], "max": [cx + x, cy + y, cz + z]},
                      {"bufferView": 2 * i + 1, "componentType": 5123, "count": len(idx), "type": "SCALAR"}]
        meshes.append({"name": name, "primitives": [{"attributes": {"POSITION": 2 * i}, "indices": 2 * i + 1, "material": i}]})
        materials.append({"name": f"mat_{i}", "doubleSided": True, **material})
        binc += vbin + ibin
    gltf = {"asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": list(range(len(boxes)))}],
            "nodes": [{"mesh": i, "name": m["name"]} for i, m in enumerate(meshes)], "meshes": meshes,
            "materials": materials, "buffers": [{"byteLength": len(binc)}], "bufferViews": views, "accessors": accessors}
    js = _json.dumps(gltf).encode()
    js += b" " * (-len(js) % 4)
    body = _struct.pack("<II", len(js), 0x4E4F534A) + js + _struct.pack("<II", len(binc), 0x004E4942) + binc
    return _struct.pack("<III", 0x46546C67, 2, 12 + len(body)) + body


# KiCad 9's `pcb export glb` materials, as in the example boards' GLBs (layout_pad / layout_silkscreen / soldermask / PCB)
_KICAD_PAD = {"pbrMetallicRoughness": {"baseColorFactor": [0.5, 0.5, 0.5, 1.0], "metallicFactor": 1.0, "roughnessFactor": 0.4}}
_KICAD_SILK = {"pbrMetallicRoughness": {"baseColorFactor": [1.0, 1.0, 1.0, 0.9], "metallicFactor": 0.0, "roughnessFactor": 0.9},
               "alphaMode": "BLEND"}
_KICAD_MASK = {"pbrMetallicRoughness": {"baseColorFactor": [0.08, 0.2, 0.14, 0.83], "metallicFactor": 0.0,
                                        "roughnessFactor": 0.6}, "alphaMode": "BLEND"}
_KICAD_CORE = {"pbrMetallicRoughness": {"baseColorFactor": [0.42, 0.45, 0.29, 0.98], "metallicFactor": 0.0,
                                        "roughnessFactor": 0.8}, "alphaMode": "BLEND"}


def _pad_and_silk_glb() -> bytes:
    """A 20 x 14 mm board, 1.6 mm thick: one 6 x 8 mm pad on the left half, one 6 x 8 mm silkscreen block on the right."""
    return _boxes_glb([
        ("layout_PCB", (0, 0, 0), (20, 1.5, 14), _KICAD_CORE),
        ("layout_soldermask", (0, 0.76, 0), (20, 0.02, 14), _KICAD_MASK),
        ("layout_pad", (-5, 0.79, 0), (6, 0.04, 8), _KICAD_PAD),
        ("layout_silkscreen", (5, 0.79, 0), (6, 0.03, 8), _KICAD_SILK),
    ])


def _harness(page: _Path, host: str, *extra: str) -> dict:
    if not (_CHROME and _shutil.which("node")):
        if _os.environ.get("PCBC_REQUIRE_CHROME"):
            raise AssertionError("PCBC_REQUIRE_CHROME is set and Chrome or node is missing")
        _pytest.skip("needs Google Chrome and node >= 22 (tools/review_host_harness.mjs)")
    out = _subprocess.run(["node", str(_HARNESS), str(page), "--host", host, "--timeout", "120", "--chrome", _CHROME, *extra],
                          capture_output=True, text=True, timeout=300)
    return _json.loads(out.stdout)


def _page(tmp_path: _Path, name: str, glb: bytes | None = None) -> _Path:
    import base64

    page = render_html(
        title="box", board_mm=(20.0, 14.0), layers=2, stackup="jlcpcb_2l_1oz", pcb_name="layout.kicad_pcb", source_text=None,
        sch_svg=None, front_svg=None, back_svg=None, copper_svg=None, silk_svg=None,
        glb_b64=base64.b64encode(glb or _box_glb()).decode(), bom_rows=[], notes=[],
    )
    p = tmp_path / name
    p.write_text(page, encoding="utf-8")
    return p


@_pytest.mark.parametrize("host", _HOSTS)
def test_the_3d_tab_shows_the_board_and_the_page_makes_no_request(tmp_path, host):
    """In every modelled host and with no host at all: the 3D box reaches "ready" and shows board pixels, nothing is
    logged as an error, the review frame's network log is empty, the host's fetch is never called and the parent gets
    no message, window.fetch keeps the exact descriptor the host (or the browser) gave it, and a later request for
    another URL still goes to that fetch."""
    r = _harness(_page(tmp_path, "box.html"), host)
    assert r["ok"], r
    assert r["state"] == "ready" and r["consoleErrors"] == [] and r["boardPixels"]["fraction"] > 0.01, r
    assert r["pageRequests"] == [] and r["hostMessages"] == 0 and r["fetchCheck"]["hostCalls"] == 0, r
    assert r["fetchUntouched"] and r["otherRequestOk"], r
    if host.startswith("getter-"):
        assert r["fetchCheck"]["fetchReads"] == 0, r


@_pytest.mark.parametrize("host", ["shim-frozen", "shim-define-late", "file"])
def test_a_viewer_that_fetches_its_model_fails_the_harness(tmp_path, monkeypatch, host):
    """Mutation: the viewer loads its model the way model-viewer did, `fetch(new Request(blob URL))` on the page's
    fetch, then parses the response. A host that pins its fetch shows the owner's exact error in the 3D tab; with no
    host the board shows but the harness sees the request. So the passes above mean the page makes no request, not that
    the harness looks away."""
    import pcbc.review as review

    parse = 'new T.GLTFLoader().parse(data, "", shown, fail);'
    assert parse in review.GLB_VIEWER
    monkeypatch.setattr(review, "GLB_VIEWER", review.GLB_VIEWER.replace(parse, (
        "window.fetch(new Request(URL.createObjectURL(new Blob([data])))).then((r) => r.arrayBuffer())"
        '.then((b) => new T.GLTFLoader().parse(b, "", shown, fail), fail);')))
    r = _harness(_page(tmp_path, "fetching.html"), host)
    assert not r["ok"], r
    if host == "file":
        assert r["state"] == "ready" and [q["url"][:5] for q in r["pageRequests"]] == ["blob:"], r
    else:
        assert r["state"] == "error" and r["fetchCheck"]["hostCalls"] == 1, r
        assert r["paneText"] == "The 3D view could not be shown. " + _OWNERS_ERROR, r


@_pytest.mark.parametrize("host", ["shim-frozen", "file"])
def test_three_s_own_loader_cannot_reach_the_network_from_this_bundle(tmp_path, monkeypatch, host):
    """Mutation: load the model through three's own loader (GLTFLoader.load of a Blob URL, i.e. FileLoader's fetch).
    In this bundle FileLoader's Headers, Request and fetch are stand-ins: the first one it meets says why in the tab,
    and neither the host's fetch nor the browser's is ever reached (no call, no message, no request), with a host or
    without."""
    import pcbc.review as review

    parse = 'new T.GLTFLoader().parse(data, "", shown, fail);'
    monkeypatch.setattr(review, "GLB_VIEWER", review.GLB_VIEWER.replace(
        parse, "new T.GLTFLoader().load(URL.createObjectURL(new Blob([data])), shown, undefined, fail);"))
    r = _harness(_page(tmp_path, "loading.html"), host)
    assert r["state"] == "error" and not r["ok"], r
    assert r["statusText"] == ("The 3D view could not be shown. Error: the pcbc review page loads nothing over the "
                               "network (Headers is not available in its 3D viewer)"), r
    assert r["pageRequests"] == [] and r["hostMessages"] == 0 and r["fetchCheck"]["hostCalls"] == 0, r


def test_a_model_that_names_an_image_is_refused_in_the_page_not_fetched(tmp_path):
    """GLTFLoader would fetch an image (or a `uri` buffer) through ImageBitmapLoader / FileLoader. KiCad's GLBs have
    none; one that does is refused with a message in the page, and still nothing is requested."""
    glb = _box_glb(extra={"images": [{"uri": "board.png"}], "textures": [{"source": 0}]})
    r = _harness(_page(tmp_path, "image.html", glb), "shim-early")
    assert r["state"] == "error" and not r["ok"], r
    assert r["statusText"] == ("The 3D view could not be shown. Error: the model refers to images or data outside the "
                               "file, which this page does not load"), r
    assert r["pageRequests"] == [] and r["hostMessages"] == 0 and r["fetchCheck"]["hostCalls"] == 0, r
    assert r["consoleErrors"] == [], r


# --- window.fetch is not even READ (checker, 2026-10-09) -------------------------------------------------------------
# three's ImageBitmapLoader ran `typeof fetch` in its constructor, and the GLTF parser builds one for every parse: a host
# that pins window.fetch behind a throwing getter broke the tab on every board. The bundle now binds three's network
# globals to bundle-local stand-ins (tools/build_three_bundle.mjs, esbuild --inject), so it has no way to reach them.

_NETWORK_GLOBAL_USE = _re.compile(r"typeof (fetch|createImageBitmap|Request|Headers|Response)\b"
                                  r"|(?<=[;,(){}=&|?:!])(fetch|createImageBitmap)\("
                                  r"|new (Request|Headers|Response)\("
                                  r"|(window|self|globalThis)\.(fetch|createImageBitmap|Request|Headers|Response)\b")


def test_the_bundle_names_no_network_global_and_carries_the_stand_ins():
    """No `typeof fetch`, no call of fetch / createImageBitmap, no `new Request|Headers|Response`, no
    `window.fetch` in the shipped bundle; the stand-ins' message is in it. The same check finds three's own code as
    esbuild bundled it before the stand-ins (the line ImageBitmapLoader and FileLoader had), so a pass is not the regex
    looking away."""
    from pcbc.review import THREE_BUNDLE

    bundle = THREE_BUNDLE.read_text(encoding="utf-8")
    assert not _NETWORK_GLOBAL_USE.search(bundle), _NETWORK_GLOBAL_USE.search(bundle)
    assert "the pcbc review page loads nothing over the network (" in bundle
    for before in ('typeof createImageBitmap>"u"&&console.warn("x"),typeof fetch>"u"&&console.warn("y")',
                   'this._abortController.signal;let l=fetch(e,a).then(function(c){return c.blob()})',
                   'let o=new Request(e,{headers:new Headers(this.requestHeader)})'):
        assert _NETWORK_GLOBAL_USE.search(before), before


@_pytest.mark.parametrize("host", ["getter-counts", "getter-throws"])
def test_a_page_that_reads_window_fetch_fails_the_read_watching_hosts(tmp_path, monkeypatch, host):
    """Mutation: the viewer does what ImageBitmapLoader did, `typeof fetch`, once. The counting host sees one read;
    the throwing host breaks the tab. So `fetchReads == 0` above means nothing in the page reads window.fetch."""
    import pcbc.review as review

    monkeypatch.setattr(review, "GLB_VIEWER", review.GLB_VIEWER.replace(
        "const data = glbBytes();", 'if (typeof fetch === "undefined") console.warn("no fetch");\n      const data = glbBytes();', 1))
    r = _harness(_page(tmp_path, "reads.html"), host)
    assert not r["ok"] and r["fetchCheck"]["fetchReads"] == 1, r
    if host == "getter-throws":
        assert r["state"] == "error" and r["statusText"] == (
            "The 3D view could not be shown. TypeError: modelled host: network is disabled in this preview"), r
    else:
        assert r["state"] == "ready" and "start" in r["fetchCheck"]["fetchReadStacks"][0], r


# --- Pads must not read as silkscreen from above (checker, 2026-10-09) -------------------------------------------------
# With three's RoomEnvironment as it comes, the top-down view (where the owner reads silkscreen orientation marks)
# mirrored its overhead light in KiCad's grey metallic pads: they came out near-white, like the silk. model-viewer had
# drawn them grey. The viewer drops that light, has metals reflect the room at 0.65 and sets a soft shadow under the
# board.

_BG = (0x0B, 0x0E, 0x0C)


def _pad_silk_shadow(shot: _Path) -> dict:
    """From a top-down screenshot of `_pad_and_silk_glb`: the median colour inside the pad (left) and the silkscreen
    (right), their distance, and the colour just outside the board's four edges (the ground shadow)."""
    from PIL import Image

    im = Image.open(shot).convert("RGB")
    w, h = im.size
    px = im.load()
    board = [(x, y) for y in range(4, h - 4, 2) for x in range(4, w - 4, 2)
             if max(abs(a - b) for a, b in zip(px[x, y], _BG)) > 24]
    x0, x1 = min(p[0] for p in board), max(p[0] for p in board)
    y0, y1 = min(p[1] for p in board), max(p[1] for p in board)

    def median(f0, f1):
        vals = [px[x, y] for y in range(int(y0 + 0.4 * (y1 - y0)), int(y0 + 0.6 * (y1 - y0)))
                for x in range(int(x0 + f0 * (x1 - x0)), int(x0 + f1 * (x1 - x0)))]
        return tuple(sorted(v[c] for v in vals)[len(vals) // 2] for c in range(3))

    pad, silk = median(0.15, 0.35), median(0.65, 0.85)
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    return {"pad": pad, "silk": silk, "distance": sum((a - b) ** 2 for a, b in zip(pad, silk)) ** 0.5,
            "outside": [px[p] for p in ((x0 - 8, cy), (x1 + 8, cy), (cx, y0 - 8), (cx, y1 + 8))]}


def _pads_are_not_silk(m: dict) -> bool:
    return (max(m["pad"]) < 200 and min(m["silk"]) > 215 and m["distance"] > 120
            and all(sum(c) <= sum(_BG) - 6 for c in m["outside"]))


def test_from_above_the_pads_are_grey_the_silkscreen_white_and_the_board_has_a_shadow(tmp_path):
    """KiCad's own pad and silkscreen materials, looked at straight down after a real drag: the pad is mid-grey (no
    channel at 200), the silkscreen near-white, more than 120 apart in RGB (measured: pad 148, silk 244, 166 apart),
    and just outside every edge of the board the background is darker than the bare background (the soft shadow)."""
    shot = tmp_path / "top.png"
    r = _harness(_page(tmp_path, "top.html", _pad_and_silk_glb()), "file", "--view", "top", "--shot", str(shot))
    assert r["ok"], r
    m = _pad_silk_shadow(shot)
    assert _pads_are_not_silk(m), m


_LIGHT_MUTANTS = {
    # three's room as it comes, metals at full strength: the pads read white (measured pad 245, silk 246)
    "as-before": [("o.visible = false;", "o.visible = true;"), ("const METAL_REFLECTION = 0.65;", "const METAL_REFLECTION = 1;")],
    "overhead-light-kept": [("o.visible = false;", "o.visible = true;")],  # pad 225, 38 from the silk
    "metals-full": [("const METAL_REFLECTION = 0.65;", "const METAL_REFLECTION = 1;")],  # pad 184, 104 from the silk
    "no-shadow": [("scene.add(groundShadow(", "(0, (")],  # the background runs right up to the board
}


@_pytest.mark.parametrize("mutant", sorted(_LIGHT_MUTANTS))
def test_each_part_of_the_lighting_is_needed(tmp_path, monkeypatch, mutant):
    """Mutation: undo each part of the lighting change (and all of it); the measurement above fails every time."""
    import pcbc.review as review

    viewer = review.GLB_VIEWER
    for old, new in _LIGHT_MUTANTS[mutant]:
        assert old in viewer, old
        viewer = viewer.replace(old, new)
    monkeypatch.setattr(review, "GLB_VIEWER", viewer)
    shot = tmp_path / "top.png"
    r = _harness(_page(tmp_path, "top.html", _pad_and_silk_glb()), "file", "--view", "top", "--shot", str(shot))
    assert r["state"] == "ready", r
    m = _pad_silk_shadow(shot)
    assert not _pads_are_not_silk(m), m


# --- The canvas follows the device pixel ratio (checker, 2026-10-09) ---------------------------------------------------
# The ratio was read once, at start: after browser zoom or moving the window to another display the view was blurry
# (or oversampled). The harness changes the ratio with the box resizing (zoom) and without (display; the page gets the
# media-query change a browser sends) and checks the canvas's backing size each time.

@_pytest.mark.parametrize("host", ["file", "http"])
def test_the_canvas_follows_the_device_pixel_ratio(tmp_path, host):
    r = _harness(_page(tmp_path, "dpr.html"), host, "--dpr", "2")
    assert r["ok"] and r["pixelRatio"]["followed"], r
    assert (r["pixelRatio"]["zoom"]["w"], r["pixelRatio"]["display"]["dpr"]) == (2 * r["pixelRatio"]["zoom"]["cssW"], 1.5), r


@_pytest.mark.parametrize("mutant", ["ratio-read-once", "no-ratio-watch"])
def test_a_canvas_that_keeps_its_first_pixel_ratio_fails(tmp_path, monkeypatch, mutant):
    """Mutation: fit() no longer sets the ratio (zoom goes blurry), or nothing listens for a ratio change without a
    resize (moving displays goes blurry)."""
    import pcbc.review as review

    old, new = {"ratio-read-once": ("    if (renderer.getPixelRatio() !== ratio()) renderer.setPixelRatio(ratio());\n", ""),
                "no-ratio-watch": ("      fit();\n      watchRatio();\n", "      fit();\n")}[mutant]
    assert review.GLB_VIEWER.count(old) == 1
    monkeypatch.setattr(review, "GLB_VIEWER", review.GLB_VIEWER.replace(old, new))
    r = _harness(_page(tmp_path, "dpr.html"), "file", "--dpr", "2")
    assert r["state"] == "ready" and not r["ok"] and not r["pixelRatio"]["followed"], r
    zoom_ok = r["pixelRatio"]["zoom"]["w"] == 2 * r["pixelRatio"]["zoom"]["cssW"]
    assert zoom_ok == (mutant == "no-ratio-watch"), r


# --- The bundle's INPUT pins, where the inputs are on this machine ---------------------------------------------------
# The output sha256 is checked above on every machine. Where three 0.179.1 and esbuild 0.25.12 are at the build
# script's default paths (or $PCBC_THREE_DIR / $PCBC_ESBUILD), the bundle is rebuilt in memory and every file esbuild
# read is checked against its pin, and the rebuilt bundle against the shipped one (`--check`).

_BUILD = _Path(__file__).resolve().parents[1] / "tools" / "build_three_bundle.mjs"


def _bundle_inputs() -> tuple[str, str]:
    three = _os.environ.get("PCBC_THREE_DIR") or str(_Path.home() / "Documents/Zener/boards/emg16-tscircuit/node_modules/three")
    esbuild = _os.environ.get("PCBC_ESBUILD") or str(_Path.home() / "Documents/MaD/Software/Control/node_modules/.bin/esbuild")
    missing = [p for p in (three + "/package.json", esbuild) if not _os.path.exists(p)]
    if missing or not _shutil.which("node"):
        _pytest.skip(f"the bundle's inputs are not on this machine ({', '.join(missing) or 'node'}); "
                     "only its output pin is checked")
    return three, esbuild


def test_the_bundle_rebuilds_from_its_pinned_inputs_to_the_shipped_bytes():
    three, esbuild = _bundle_inputs()
    out = _subprocess.run(["node", str(_BUILD), "--check", "--three", three, "--esbuild", esbuild],
                          capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "three-0.179.1-viewer.min.js matches; three-0.179.1.LICENSE matches" in out.stdout, out.stdout


def test_a_changed_input_pin_stops_the_build(tmp_path):
    """Mutation: one input's pin is wrong (as if GLTFLoader.js had changed under it): the check names that input and
    fails, and nothing is written."""
    three, esbuild = _bundle_inputs()
    script = _BUILD.read_text()
    pin = _re.search(r'"examples/jsm/loaders/GLTFLoader.js": "([0-9a-f]{64})"', script).group(1)
    mutant = tmp_path / "build_three_bundle.mjs"
    mutant.write_text(script.replace(pin, "0" * 64))
    for mode in (["--check"], []):
        out = _subprocess.run(["node", str(mutant), *mode, "--three", three, "--esbuild", esbuild, "--out", str(tmp_path)],
                              capture_output=True, text=True, timeout=300)
        assert out.returncode == 1, out.stdout + out.stderr
        assert f"input examples/jsm/loaders/GLTFLoader.js: pinned {'0' * 64} got {pin}" in out.stderr, out.stderr
    assert sorted(p.name for p in tmp_path.iterdir()) == ["build_three_bundle.mjs"], "nothing written"


# ---------------------------------------------------------------------------------------------
# The page with JavaScript off. The owner opened a review page in Claude's phone app and could not leave the
# schematic: the tabs were switched only by a script, and that viewer shows the page with scripts off (the lead saw
# the tabs work in a phone-sized Chrome with scripts on). The full page's tabs are radio buttons + labels + CSS now and
# its schematic leaves pinch and scroll to the phone; its 3D tab says the interactive view needs JavaScript and points
# at static.html, the page made for that viewer (below). tools/review_tabs_check.mjs taps every tab in a 375x812 touch
# Chrome (Emulation.setScriptExecutionDisabled for --nojs) and reads the result through the DOM and CSS domains.

_TABS_CHECK = _Path(__file__).resolve().parents[1] / "tools" / "review_tabs_check.mjs"
_ZOOM_CHECK = _Path(__file__).resolve().parents[1] / "tools" / "review_zoom_check.mjs"
_STATIC_CHECK = _Path(__file__).resolve().parents[1] / "tools" / "review_static_check.mjs"
_SCH_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="2000px" height="1400px" viewBox="0 0 200 140">'
            '<rect x="10" y="10" width="180" height="120" fill="none" stroke="#800" stroke-width="2"/>'
            '<text x="20" y="40" font-size="12">R1 1k</text></svg>')
_PCB_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="40mm" height="25mm" viewBox="0 0 40 25">'
            '<rect x="1" y="1" width="38" height="23" fill="none" stroke="#c83" stroke-width="0.3"/></svg>')
_LONG_PATH = "/a/very/long/absolute/path/that/runs/off/a/phone/screen/without/wrapping/layout/box/fab/layout.kicad_pcb"
_BOM = [["Comment", "Designator", "Footprint", "LCSC Part #"],
        ["10uF_25V_X5R_0805_long_value_name", "C1,C2,C3,C4,C5,C6", "C_0805_2012Metric_long", "C15850"]]


def _jpeg(w=400, h=300, rgb=(36, 70, 52)) -> str:
    import base64
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), rgb).save(buf, "JPEG")
    return base64.b64encode(buf.getvalue()).decode()


def _full_page(tmp_path: _Path, name: str = "full.html", *, glb=True) -> _Path:
    import base64

    page = render_html(
        title="box", board_mm=(20.0, 14.0), layers=2, stackup="jlcpcb_2l_1oz", pcb_name=_LONG_PATH,
        source_text="Board(width=20, height=14)\n", sch_svg=_SCH_SVG, front_svg=_PCB_SVG, back_svg=_PCB_SVG,
        copper_svg=_PCB_SVG, silk_svg=_PCB_SVG, glb_b64=base64.b64encode(_box_glb()).decode() if glb else None,
        bom_rows=_BOM, notes=["ok"],
    )
    p = tmp_path / name
    p.write_text(page, encoding="utf-8")
    return p


def _tabs(page: _Path, *extra: str) -> dict:
    if not (_CHROME and _shutil.which("node")):
        if _os.environ.get("PCBC_REQUIRE_CHROME"):
            raise AssertionError("PCBC_REQUIRE_CHROME is set and Chrome or node is missing")
        _pytest.skip("needs Google Chrome and node >= 22 (tools/review_tabs_check.mjs)")
    out = _subprocess.run(["node", str(_TABS_CHECK), str(page), "--chrome", _CHROME, "--timeout", "120", *extra],
                          capture_output=True, text=True, timeout=300)
    return _json.loads(out.stdout)


def test_the_page_carries_its_views_as_radio_state_and_its_3d_tab_points_at_the_static_page(tmp_path):
    from pcbc.review import TABS

    page = _full_page(tmp_path).read_text()
    radios = _re.findall(r'<input type="radio" class="tab-state" name="view" id="tab-(\w+)"[^>]*>', page)
    assert radios == [k for k, _ in TABS]
    assert len(_re.findall(r'class="tab-state"[^>]* checked', page)) == 1 and 'id="tab-sch" autocomplete="off" checked' in page
    for key, _ in TABS:
        assert f'<label for="tab-{key}" data-tab="{key}">' in page and f"#tab-{key}:checked ~ #{key} {{ display: block; }}" in page
    assert '<div id="board-3d" class="viewer3d" data-state="idle" hidden>' in page, "the empty viewer box waits for the script"
    assert 'id="board-3d-nojs">The interactive 3D view needs JavaScript' in page and "static.html" in page
    assert "<img" not in page, "the 3D still renders live in static.html, not in the full page"


def _zoom(page: _Path) -> dict:
    if not (_CHROME and _shutil.which("node")):
        if _os.environ.get("PCBC_REQUIRE_CHROME"):
            raise AssertionError("PCBC_REQUIRE_CHROME is set and Chrome or node is missing")
        _pytest.skip("needs Google Chrome and node >= 22 (tools/review_zoom_check.mjs)")
    out = _subprocess.run(["node", str(_ZOOM_CHECK), str(page), "--chrome", _CHROME],
                          capture_output=True, text=True, timeout=120)
    try:
        return _json.loads(out.stdout)
    except _json.JSONDecodeError as e:
        raise AssertionError(f"zoom check did not print JSON ({e}):\n{out.stdout}\n{out.stderr}") from e


def test_the_schematic_refits_after_the_window_changes_while_its_tab_is_hidden(tmp_path):
    """Hiding the schematic, resizing, and showing it again fits the new box. A layout made while the
    tab was hidden stuck: buck came back 1 px wide, and c3_usb and node kept the previous width."""
    r = _zoom(_full_page(tmp_path, glb=False))
    assert r["ok"], r


def test_without_relayout_a_hidden_resize_leaves_the_old_sheet(tmp_path):
    """Mutation: relayout does nothing. The first fit stands, and showing the schematic after a
    resize while it was hidden does not pick up the new box. The show listener, the resize observer
    and the window resize all go through relayout, and any one of them can fire."""
    needle = (
        "    function relayout() {\n"
        "      if (scale !== 1) return;\n"
        '      if (!document.getElementById("tab-sch").checked) return;\n'
        "      if (!fitted()) return;\n"
        "      layout();\n"
        "    }\n"
    )
    page = _full_page(tmp_path, "mut.html", glb=False).read_text()
    assert needle in page
    p = tmp_path / "mut.html"
    p.write_text(page.replace(needle, "    function relayout() { return; }\n", 1), encoding="utf-8")
    r = _zoom(p)
    assert not r["ok"], r
    assert any("after resize while hidden" in f for f in r["failures"]), r


@_pytest.mark.parametrize("js", ["nojs", "js"])
def test_every_tab_opens_with_a_tap_on_a_phone_with_and_without_javascript(tmp_path, js):
    """375x812 touch Chrome: each tap shows that tab's section only and marks its label, nothing scrolls sideways (the
    long board path wraps), the pictures fit the screen, ArrowRight on the focused tab moves on with a visible outline.
    Without JS: the schematic leaves pinch/scroll to the phone and the 3D tab shows its note, not an empty box. With
    JS: the zoom script runs and the interactive 3D view replaces the note and reaches "ready"."""
    r = _tabs(_full_page(tmp_path), *(["--nojs"] if js == "nojs" else []))
    assert r["ok"], r
    assert r["scriptsRan"] is (js == "js"), r
    assert [t["tab"] for t in r["tabs"]] == ["sch", "front", "silk", "back", "both", "three", "bom", "src", "sch"], r
    three = next(t for t in r["tabs"] if t["tab"] == "three")["three"]
    if js == "nojs":
        assert three["note"] and not three["box"], r
        assert next(t for t in r["tabs"] if t["tab"] == "sch")["schTouchAction"] != "none", r
    else:
        assert three["box"] and not three["note"] and three["state"] == "ready", r


def _drop_tab_css(page: str) -> str:
    return _re.sub(r"  #tab-\w+:checked ~ #\w+ \{ display: block; \}\n", "", page)


@_pytest.mark.parametrize("mutant", ["no-css-state", "script-tabs", "sch-blocks-touch"])
def test_without_the_css_tab_state_a_phone_with_scripts_off_is_stuck(tmp_path, mutant):
    """Mutations. no-css-state: the `:checked ~ section` rules are removed (no tap shows anything). script-tabs: the
    old page's mechanism, a script toggling sections, with no CSS state; with scripts off every tap leaves the
    schematic up, the owner's report. sch-blocks-touch: the schematic box keeps `touch-action: none` without JS, so
    the phone cannot pinch or scroll it."""
    page = _full_page(tmp_path).read_text()
    if mutant == "no-css-state":
        page = _drop_tab_css(page)
    elif mutant == "script-tabs":
        page = _drop_tab_css(page).replace("  section { display: none;", "  section.active { display: block; }\n  section { display: none;")
        page = page.replace('<section id="sch">', '<section id="sch" class="active">').replace("</body>", """<script>
  document.querySelectorAll("nav [data-tab]").forEach(b => b.addEventListener("click", (e) => {
    e.preventDefault();
    document.querySelectorAll("section").forEach(s => s.classList.toggle("active", s.id === b.dataset.tab));
  }));
</script></body>""")
    else:
        page = page.replace("</style>", "  .plot.sch { touch-action: none; }\n</style>", 1)
    p = tmp_path / f"{mutant}.html"
    p.write_text(page, encoding="utf-8")
    r = _tabs(p, "--nojs")
    assert not r["ok"], r
    if mutant == "sch-blocks-touch":
        assert set(r["failures"]) == {"without JS the schematic box blocks pinch and scroll (touch-action: none)"}, r
    else:
        assert "tap front: shows " + ("" if mutant == "no-css-state" else "sch") in r["failures"], r


@_pytest.mark.parametrize("host", ["file", "shim-frozen", "getter-throws"])
def test_the_full_page_with_radio_tabs_still_makes_no_request(tmp_path, host):
    """The full page as review writes it now (radio tabs, every picture, BOM, board.py): the host harness still clicks
    its 3D tab, the view reaches "ready" and the frame makes no request at all."""
    r = _harness(_full_page(tmp_path), host)
    assert r["ok"] and r["state"] == "ready" and r["pageRequests"] == [], r


def test_an_image_a_script_loads_counts_as_a_request(tmp_path):
    """Mutation: a script on the full page loads a data: image: the harness reports it (only the document itself is
    not a request)."""
    page = _full_page(tmp_path).read_text().replace("</body>", """<script>
  new Image().src = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==";
</script></body>""")
    p = tmp_path / "extra-image.html"
    p.write_text(page, encoding="utf-8")
    r = _harness(p, "file")
    assert not r["ok"] and [q["type"] for q in r["pageRequests"]] == ["Image"], r


# ---------------------------------------------------------------------------------------------
# static.html: the review as pictures for a viewer that runs no JavaScript (the owner: "a static version for phone ...
# that produces images including 3d", the full page kept as the future review page, the review command giving both).
# No <script> at all, its own CSP (default-src 'none'; img-src data:), every picture a `data:` URI inside the one file:
# KiCad's SVGs for the schematic and board plots (sharp at any pinch zoom), JPEG for kicad-cli's 3D renders.
# `static_page_problems` holds a page to that in Python; tools/review_static_check.mjs opens it in Chrome with scripts
# off as a 375x812 touch phone and at 1280x900 and checks each picture DECODED (naturalWidth > 0) and nothing overflows.

_STILL_CAPTIONS = ("Top", "Bottom, seen from below", "Angled, from the front left", "Angled, from the back right")


def _static_text(**over) -> str:
    from pcbc.review import render_static_html

    kw = dict(
        title="box",
        facts=[("Size", "20 × 14 mm (declared outline)", False), ("Nets", "3 of 4 routed · 1 unrouted: VBUS", True),
               ("Board file", _LONG_PATH, False)],
        sch_svg=_SCH_SVG, front_svg=_PCB_SVG, back_svg=_PCB_SVG, silk_svg=_PCB_SVG, copper_svg=_PCB_SVG,
        stills=[(c, "image/jpeg", _jpeg(rgb=(36, 70 + 20 * i, 52))) for i, c in enumerate(_STILL_CAPTIONS)],
        bom_rows=_BOM, notes=["ok"],
    )
    kw.update(over)
    return render_static_html(**kw)


def _static_page(tmp_path: _Path, text: str | None = None, name: str = "static.html") -> _Path:
    p = tmp_path / name
    p.write_text(_static_text() if text is None else text, encoding="utf-8")
    return p


def test_the_static_page_has_no_script_and_every_picture_decodes():
    import base64
    import io
    import xml.etree.ElementTree as ET

    from PIL import Image

    from pcbc.review import STATIC_CSP, static_page_problems

    page = _static_text()
    assert static_page_problems(page) == []
    assert "<script" not in page.lower() and f'content="{STATIC_CSP}"' in page
    assert not _re.search(r"""(?:src|href)=["'](?!data:|#)""", page), "nothing but data: pictures and in-page anchors"
    imgs = _re.findall(r'<img src="data:(image/[\w+.-]+);base64,([A-Za-z0-9+/=]+)"', page)
    assert [m for m, _ in imgs] == ["image/svg+xml"] * 5 + ["image/jpeg"] * 4
    for mime, b64 in imgs:
        data = base64.b64decode(b64)
        if mime == "image/svg+xml":
            assert ET.fromstring(data).tag.endswith("svg")
        else:
            assert Image.open(io.BytesIO(data)).size == (400, 300)
    for cap in _STILL_CAPTIONS:
        assert f"<figcaption>{cap}</figcaption>" in page
    for sid in ("status", "sch", "front", "back", "silk", "both", "three", "bom"):
        assert f'id="{sid}"' in page
    assert "<td>C15850</td>" in page and "1 unrouted: VBUS" in page


_STATIC_MUTANTS = {
    "script": (lambda p: p.replace("</body>", "<script>document.title='x'</script></body>"), "has a <script>"),
    "handler": (lambda p: p.replace("<img ", '<img onerror="x()" ', 1), "has an event handler"),
    "missing-picture": (lambda p: _re.sub(r'<figure class="pic"><img [^>]+/></figure>', "", p, count=1),
                        "section front shows 0 of 1 pictures"),
    "broken-picture": (lambda p: _re.sub(r"data:image/jpeg;base64,[A-Za-z0-9+/=]+", "data:image/jpeg;base64,/9j/AAAA", p, count=1),
                       "a picture in section three: not a whole JPEG"),
    "network-picture": (lambda p: p.replace('<p class="toc">', '<img src="http://127.0.0.1:9/x.png" alt=""/><p class="toc">'),
                        "names 'http://127.0.0.1:9/x.png'"),
    "no-csp": (lambda p: p.replace('http-equiv="Content-Security-Policy"', 'http-equiv="X-None"'),
               "does not carry its Content-Security-Policy"),
}


@_pytest.mark.parametrize("mutant", sorted(_STATIC_MUTANTS))
def test_a_script_or_a_missing_or_broken_picture_fails_the_static_page(mutant):
    from pcbc.review import static_page_problems

    mutate, want = _STATIC_MUTANTS[mutant]
    problems = static_page_problems(mutate(_static_text()))
    assert any(want in p for p in problems), problems


def test_an_svg_that_refers_outside_itself_is_not_put_on_the_static_page():
    from pcbc.review import static_page_problems

    bad = _PCB_SVG.replace("</svg>", '<image href="https://example.com/logo.png" width="5" height="5"/></svg>')
    page = _static_text(front_svg=bad)
    front = page[page.index('<section id="front">'):page.index('<section id="back">')]
    assert "<img" not in front and "refers to something outside itself: https://example.com/logo.png" in front
    assert "section front shows 0 of 1 pictures" in static_page_problems(page)


def _static_check(page: _Path, *extra: str) -> dict:
    if not (_CHROME and _shutil.which("node")):
        if _os.environ.get("PCBC_REQUIRE_CHROME"):
            raise AssertionError("PCBC_REQUIRE_CHROME is set and Chrome or node is missing")
        _pytest.skip("needs Google Chrome and node >= 22 (tools/review_static_check.mjs)")
    out = _subprocess.run(["node", str(_STATIC_CHECK), str(page), "--chrome", _CHROME, "--timeout", "120", *extra],
                          capture_output=True, text=True, timeout=300)
    return _json.loads(out.stdout)


def test_the_static_page_shows_every_section_decoded_on_a_phone_and_a_desktop_with_scripts_off(tmp_path):
    r = _static_check(_static_page(tmp_path), "--shots", str(tmp_path / "shots"))
    assert r["ok"], r
    for name, vw in (("phone", 375), ("desktop", 1280)):
        v = r["views"][name]
        assert v["scripts"] == 0 and v["images"] == v["decoded"] == 9 and v["scrollWidth"] == v["vw"] == vw, v
    assert (tmp_path / "shots" / "static-phone.png").stat().st_size > 1000


@_pytest.mark.parametrize("mutant", ["script", "broken-picture", "missing-picture", "too-wide", "network-picture", "no-zoom"])
def test_a_static_page_chrome_cannot_show_whole_fails_the_check(tmp_path, mutant):
    """Mutations, each caught in Chrome with scripts off: a <script> element (present even though it cannot run), a
    picture that does not decode, a section without its picture, a line wider than the phone, a picture from the
    network (the page's CSP blocks it, so it does not decode either), a viewport that stops pinch zoom."""
    page = _static_text()
    want = {
        "script": "<script> element",
        "broken-picture": "did not decode",
        "missing-picture": "section front has 0 of 1 pictures",
        "too-wide": "px wide on a 375px screen",
        "network-picture": "did not decode",
        "no-zoom": "stops pinch zoom",
    }[mutant]
    if mutant in ("script", "broken-picture", "missing-picture", "network-picture"):
        page = _STATIC_MUTANTS[mutant][0](page)
    elif mutant == "too-wide":
        page = page.replace("<dl>", '<div style="width: 900px">wide</div><dl>')
    else:
        page = page.replace('content="width=device-width, initial-scale=1"', 'content="width=device-width, initial-scale=1, user-scalable=no"')
    r = _static_check(_static_page(tmp_path, page, f"{mutant}.html"))
    assert not r["ok"] and any(want in f for f in r["failures"]), r


def test_the_review_command_names_both_pages(tmp_path, monkeypatch, capsys):
    import pcbc.cli as cli

    board = tmp_path / "b.py"
    board.write_text("")
    pages = {"html": str(tmp_path / "review" / "index.html"), "static_html": str(tmp_path / "review" / "static.html"),
             "static_bytes": 1234, "static_problems": [], "pcb": "x.kicad_pcb", "error": None, "steps": []}
    monkeypatch.setattr(cli, "review_job", lambda path, open_html=True: dict(pages))
    assert cli.main(["review", str(board), "--no-open"]) == 0
    out = _json.loads(capsys.readouterr().out)
    assert out["html"].endswith("review/index.html") and out["static_html"].endswith("review/static.html"), out


@_pytest.mark.kicad
def test_review_renders_four_still_pictures_of_the_board(tmp_path):
    """The static page's 3D pictures come from kicad-cli pcb render: top, bottom and two angled views, JPEG at about
    the asked 1200x900 (KiCad 10.0.6 writes 1176x872 for that), made side by side, each a different picture."""
    import base64
    import io

    from PIL import Image

    from conftest import placed_board
    from pcbc.fab import kicad_cli
    from pcbc.review import STILL_SIZE, _render_stills

    blinky = _Path(__file__).resolve().parents[1] / "examples" / "blinky" / "blinky.py"
    steps, stills = _render_stills(kicad_cli(), placed_board("blinky", blinky), tmp_path)
    assert [s["returncode"] for s in steps] == [0, 0, 0, 0], steps
    assert tuple(c for c, _, _ in stills) == _STILL_CAPTIONS
    seen = set()
    for _, mime, b64 in stills:
        img = Image.open(io.BytesIO(base64.b64decode(b64)))
        assert mime == "image/jpeg" and img.format == "JPEG"
        assert all(0.95 * want <= got <= want for got, want in zip(img.size, STILL_SIZE)), img.size
        small = img.convert("L").resize((16, 12)).tobytes()
        assert len(set(small)) > 10, "a picture, not a blank"
        seen.add(small)
    assert len(seen) == 4, "four different views"
