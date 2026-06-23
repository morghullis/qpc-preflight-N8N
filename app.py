"""
QPC Preflight Microservice
==========================
Analiza archivos de impresion (PDF e imagenes) con precision de prepress.
Recibe MUCHOS archivos en una sola llamada y devuelve el analisis de CADA uno.

Mide de verdad: tamano exacto (trim/media), sangrado (bleed), paginas,
DPI real de cada imagen embebida (detecta baja calidad), contenido pegado
al borde, fuentes embebidas, espacio de color, y (opcional) revision visual
con Claude para "letras pegadas/cortadas".

Stack: FastAPI + PyMuPDF (fitz) + Pillow.
"""
import io, os, base64, json, traceback
from typing import List, Optional

import fitz  # PyMuPDF
from PIL import Image

# ---------- helpers ----------
def inches(pts: float) -> float:
    return round(pts / 72.0, 3)

def near_white(rgb, thresh=245):
    return all(c >= thresh for c in rgb[:3])

# Umbrales de DPI segun tamano del trabajo (gran formato tolera menos DPI)
def dpi_targets(max_dim_in: float):
    if max_dim_in >= 24:   # banners / signage grande
        return 100, 150    # (minimo aceptable, ideal)
    if max_dim_in >= 13:   # posters medianos
        return 150, 200
    return 200, 300        # tarjetas, flyers, etc.

# ---------- PDF ----------
def analyze_pdf(data: bytes, target=None, do_vision=False):
    res = {"kind": "pdf", "issues": [], "warnings": []}
    doc = fitz.open(stream=data, filetype="pdf")
    res["pages"] = doc.page_count
    page = doc[0]

    media = page.mediabox
    trim = page.trimbox
    bleed_box = page.bleedbox
    crop = page.cropbox
    res["media_in"] = {"w": inches(media.width), "h": inches(media.height)}
    res["trim_in"]  = {"w": inches(trim.width),  "h": inches(trim.height)}
    # tamano "real" del trabajo = trimbox si existe distinto, si no mediabox
    size = res["trim_in"] if (trim.width and trim.height) else res["media_in"]
    res["size_in"] = size
    max_dim = max(size["w"], size["h"])
    dpi_min, dpi_ideal = dpi_targets(max_dim)
    res["dpi_min"], res["dpi_ideal"] = dpi_min, dpi_ideal

    # sangrado: bleedbox o mediabox mas grande que trimbox
    outer = bleed_box if (bleed_box.width > trim.width or bleed_box.height > trim.height) else media
    bleed_w = (outer.width - trim.width) / 2.0
    bleed_h = (outer.height - trim.height) / 2.0
    bleed_amt = round(min(bleed_w, bleed_h) / 72.0, 3)
    bleed_present = bleed_amt >= 0.05  # ~1/16in o mas
    res["bleed"] = {"present": bleed_present, "amount_in": max(bleed_amt, 0.0)}

    # imagenes embebidas -> DPI efectivo real
    imgs_out, min_dpi = [], None
    seen = set()
    for im in page.get_images(full=True):
        xref, w, h, cs = im[0], im[2], im[3], im[5]
        if xref in seen:
            continue
        seen.add(xref)
        eff = None
        try:
            rects = page.get_image_rects(xref)
            if rects:
                rr = rects[0]
                if rr.width and rr.height:
                    eff = round(min(w / (rr.width/72.0), h / (rr.height/72.0)))
        except Exception:
            pass
        low = (eff is not None and eff < dpi_min)
        if eff is not None:
            min_dpi = eff if min_dpi is None else min(min_dpi, eff)
        imgs_out.append({"px": [w, h], "eff_dpi": eff, "colorspace": cs, "low_res": low})
    res["images"] = imgs_out
    res["min_image_dpi"] = min_dpi
    res["low_res_images"] = [i for i in imgs_out if i["low_res"]]

    # fuentes embebidas
    fonts_out, all_emb = [], True
    for f in page.get_fonts(full=True):
        ext, ftype, base = f[1], f[2], f[3]
        emb = ext not in ("", "n/a", None)
        if not emb:
            all_emb = False
        fonts_out.append({"name": base, "type": ftype, "embedded": emb})
    res["fonts"] = fonts_out
    res["fonts_all_embedded"] = all_emb if fonts_out else True

    # color
    spaces = sorted({i["colorspace"] for i in imgs_out if i.get("colorspace")})
    has_rgb = any("RGB" in s for s in spaces)
    res["color"] = {"spaces": spaces, "has_rgb": has_rgb}

    # contenido pegado al borde (render + anillo de pixeles)
    try:
        dpi = 100
        pix = page.get_pixmap(dpi=dpi)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples) if pix.n < 4 \
              else Image.frombytes("RGBA", [pix.width, pix.height], pix.samples).convert("RGB")
        band = max(2, round(0.125 * dpi))  # banda de 1/8in
        W, H = img.size
        px = img.load()
        def band_has_content(region):
            x0, y0, x1, y1 = region
            step = 2
            for y in range(y0, y1, step):
                for x in range(x0, x1, step):
                    if not near_white(px[x, y]):
                        return True
            return False
        edge = (band_has_content((0,0,W,band)) or band_has_content((0,H-band,W,H)) or
                band_has_content((0,0,band,H)) or band_has_content((W-band,0,W,H)))
        res["content_at_edge"] = edge
        res["needs_bleed"] = bool(edge and not bleed_present)
    except Exception as e:
        res["content_at_edge"] = None
        res["needs_bleed"] = None
        res["warnings"].append("no se pudo renderizar para revisar el borde: %s" % e)

    # vision opcional con Claude (letras pegadas/cortadas, calidad visual)
    if do_vision and os.environ.get("ANTHROPIC_API_KEY"):
        try:
            res["vision"] = _vision_check(page)
        except Exception as e:
            res["vision"] = {"checked": False, "error": str(e)}
    else:
        res["vision"] = {"checked": False}

    doc.close()

    # construir issues legibles
    if res["low_res_images"]:
        worst = min(i["eff_dpi"] for i in res["low_res_images"])
        res["issues"].append(f"Imagen(es) a baja resolucion (la mas baja ~{worst} DPI; minimo recomendado {dpi_min} DPI a este tamano).")
    if res["needs_bleed"]:
        res["issues"].append("El arte llega al borde pero el archivo no trae sangrado (bleed). Se recomienda 0.125in de sangrado por lado.")
    if not res["fonts_all_embedded"]:
        res["issues"].append("Hay fuentes NO embebidas. Conviene convertir el texto a curvas o embeber las fuentes.")
    if res.get("vision", {}).get("issues"):
        res["issues"].extend(res["vision"]["issues"])

    res["severity"] = "fail" if res["issues"] else ("warn" if res["warnings"] else "ok")
    res["ok"] = (res["severity"] != "fail")
    return res

def _vision_check(page):
    import anthropic
    pix = page.get_pixmap(dpi=120)
    png = pix.tobytes("png")
    b64 = base64.standard_b64encode(png).decode()
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=400,
        messages=[{"role":"user","content":[
            {"type":"image","source":{"type":"base64","media_type":"image/png","data":b64}},
            {"type":"text","text":(
                "Eres revisor de prepress. Mira SOLO problemas visuales evidentes para impresion: "
                "texto cortado por el borde, letras encimadas/pegadas ilegibles, elementos importantes "
                "demasiado cerca del filo, o imagenes claramente pixeladas/borrosas. "
                "Responde JSON: {\"issues\":[\"...\"]} en espanol. Si no hay problemas, issues vacio. Solo JSON.")}
        ]}]
    )
    txt = "".join(b.text for b in msg.content if getattr(b,"type",None)=="text").strip()
    txt = txt.replace("```json","").replace("```","").strip()
    try:
        data = json.loads(txt)
        return {"checked": True, "issues": data.get("issues", [])}
    except Exception:
        return {"checked": True, "issues": [], "raw": txt[:300]}

# ---------- IMAGE ----------
def analyze_image(data: bytes, target=None):
    res = {"kind": "image", "issues": [], "warnings": []}
    img = Image.open(io.BytesIO(data))
    w, h = img.size
    res["px"] = [w, h]
    res["mode"] = img.mode
    dpi = img.info.get("dpi")
    res["embedded_dpi"] = [round(dpi[0]), round(dpi[1])] if dpi else None
    res["color"] = {"spaces": [img.mode], "has_rgb": img.mode in ("RGB","RGBA")}

    if target and target.get("w") and target.get("h"):
        eff_w = round(w / target["w"]); eff_h = round(h / target["h"])
        eff = min(eff_w, eff_h)
        max_dim = max(target["w"], target["h"])
        dpi_min, dpi_ideal = dpi_targets(max_dim)
        res["target_in"] = target
        res["eff_dpi_at_target"] = eff
        res["dpi_min"], res["dpi_ideal"] = dpi_min, dpi_ideal
        if eff < dpi_min:
            res["issues"].append(f"A {target['w']}x{target['h']}in la imagen queda a ~{eff} DPI (minimo {dpi_min}). Se vera pixelada.")
    else:
        res["note"] = "Sin tamano objetivo no se puede calcular DPI efectivo; envia las medidas para evaluar calidad."
    res["severity"] = "fail" if res["issues"] else "ok"
    res["ok"] = (res["severity"] != "fail")
    return res

# ---------- dispatcher ----------
def analyze_one(filename: str, data: bytes, target=None, do_vision=False):
    name = (filename or "archivo").strip()
    low = name.lower()
    try:
        if low.endswith(".pdf") or data[:5] == b"%PDF-":
            r = analyze_pdf(data, target, do_vision)
        elif low.endswith((".png",".jpg",".jpeg",".tif",".tiff",".webp",".bmp",".gif")) or data[:3]==b"\xff\xd8\xff" or data[:8]==b"\x89PNG\r\n\x1a\n":
            r = analyze_image(data, target)
        elif low.endswith((".doc",".docx",".pages",".pub",".ppt",".pptx")):
            r = {"kind":"other","ok":False,"severity":"fail",
                 "issues":["Es un documento editable, no un archivo listo para imprimir. Exporta a PDF de alta calidad."],"warnings":[]}
        else:
            r = {"kind":"other","ok":False,"severity":"warn",
                 "issues":[],"warnings":["Tipo de archivo no reconocido para preflight."]}
    except Exception as e:
        r = {"kind":"error","ok":False,"severity":"fail","issues":["No se pudo analizar el archivo: %s"%e],"warnings":[],
             "trace": traceback.format_exc()[-500:]}
    r["filename"] = name
    # resumen corto por archivo
    sz = r.get("size_in") or ({"w":r["px"][0],"h":r["px"][1]} if r.get("px") else None)
    r["summary"] = _summary(r)
    return r

def _summary(r):
    k = r.get("kind")
    if k == "pdf":
        s = f"PDF {r['size_in']['w']}x{r['size_in']['h']}in, {r['pages']} pag."
        if r.get("min_image_dpi") is not None:
            s += f" Imagen mas baja ~{r['min_image_dpi']} DPI."
        s += " Bleed: " + ("si" if r.get("bleed",{}).get("present") else "no") + "."
        if r["issues"]: s += " ⚠️ " + " ".join(r["issues"])
        else: s += " ✅ Sin problemas detectados."
        return s
    if k == "image":
        s = f"Imagen {r['px'][0]}x{r['px'][1]}px ({r.get('mode')})."
        if r.get("eff_dpi_at_target"): s += f" ~{r['eff_dpi_at_target']} DPI al tamano pedido."
        s += (" ⚠️ " + " ".join(r["issues"])) if r["issues"] else " ✅"
        return s
    return (r.get("issues") or r.get("warnings") or ["Sin detalle"])[0]

# ============ SERVIDOR FASTAPI ============
from fastapi import FastAPI, UploadFile, File, Form, Body
from fastapi.responses import JSONResponse
from typing import List, Optional

app = FastAPI(title="QPC Preflight", version="1.0")

def _combine(results):
    fails = [r for r in results if r.get("severity") == "fail"]
    warns = [r for r in results if r.get("severity") == "warn"]
    overall_ok = len(fails) == 0
    if not results:
        summ = "No se recibieron archivos."
    elif overall_ok and not warns:
        summ = f"{len(results)} archivo(s) revisado(s): todos OK para impresion."
    else:
        summ = f"{len(results)} archivo(s) revisado(s): {len(fails)} con problemas, {len(warns)} con avisos."
    return {"file_count": len(results), "overall_ok": overall_ok,
            "summary": summ, "files": results}

def _clean_b64(s: str) -> bytes:
    if not s:
        return b""
    if "," in s[:64] and s[:5] in ("data:", "DATA:"):
        s = s.split(",", 1)[1]
    return base64.b64decode(s)

@app.get("/")
def health():
    return {"service": "qpc-preflight", "status": "ok",
            "vision_enabled": bool(os.environ.get("ANTHROPIC_API_KEY"))}

# --- Modo JSON (lo que usa n8n): manda todos los archivos en base64 de una vez ---
@app.post("/preflight-json")
def preflight_json(payload: dict = Body(...)):
    files = payload.get("files", []) or []
    target = payload.get("target")  # {"w":24,"h":18} opcional
    do_vision = bool(payload.get("vision", False))
    results = []
    for f in files:
        data = _clean_b64(f.get("content_base64", ""))
        results.append(analyze_one(f.get("filename", "archivo"), data, target, do_vision))
    return JSONResponse(_combine(results))

# --- Modo multipart (para probar con curl o subir archivos a mano) ---
@app.post("/preflight")
async def preflight(files: List[UploadFile] = File(...),
                    target_width_in: Optional[float] = Form(None),
                    target_height_in: Optional[float] = Form(None),
                    vision: bool = Form(False)):
    target = None
    if target_width_in and target_height_in:
        target = {"w": float(target_width_in), "h": float(target_height_in)}
    results = []
    for uf in files:
        data = await uf.read()
        results.append(analyze_one(uf.filename, data, target, bool(vision)))
    return JSONResponse(_combine(results))

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
