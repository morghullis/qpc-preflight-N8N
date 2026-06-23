# QPC Preflight — Microservicio de análisis de archivos de impresión

Analiza archivos PDF e imágenes con **precisión de prepress** y revisa **todos
los archivos** que reciba en una sola llamada (1, 5 o 30 — no importa).

## Qué mide (de verdad, no adivinando)
- **Tamaño exacto** del trabajo (TrimBox / MediaBox en pulgadas).
- **Sangrado (bleed):** si lo trae y cuánto.
- **Páginas** del PDF.
- **DPI real de cada imagen embebida** → detecta imágenes de baja calidad.
- **Contenido pegado al borde** (render + análisis de píxeles) → avisa si falta bleed.
- **Fuentes embebidas** (o no).
- **Espacio de color** (CMYK / RGB / Separation).
- **(Opcional) Revisión visual con Claude:** letras pegadas/cortadas, borroso.
  Solo si pones la variable `ANTHROPIC_API_KEY`. Si la dejas vacía, todo lo demás
  funciona igual.

## Endpoints
- `GET /` → health check. Debe responder `{"service":"qpc-preflight","status":"ok"}`.
- `POST /preflight-json` → **el que usa n8n**. Body JSON:
  ```json
  {
    "files": [
      {"filename": "arte.pdf", "content_base64": "JVBERi0xLj..."},
      {"filename": "logo.png", "content_base64": "iVBORw0KGgo..."}
    ],
    "target": {"w": 24, "h": 18},   // opcional: medida pedida (para DPI de imágenes)
    "vision": false                  // opcional: true para revisión visual con Claude
  }
  ```
  Devuelve `{ file_count, overall_ok, summary, files:[ {por cada archivo...} ] }`.
- `POST /preflight` → igual pero multipart (para probar a mano con curl).

---

## Cómo desplegarlo en Railway (lo más fácil, ~10 min)

1. **Sube estos archivos a un repo de GitHub** (app.py, requirements.txt, Dockerfile,
   .env.example, README.md). Puedes arrastrarlos en github.com → "Add file" → "Upload files".
2. Entra a **railway.app** → **New Project** → **Deploy from GitHub repo** → elige el repo.
3. Railway detecta el **Dockerfile** y construye solo. Espera ~2–3 min.
4. **Settings → Networking → Generate Domain.** Te da una URL pública tipo
   `https://qpc-preflight-production.up.railway.app`.
5. *(Opcional)* **Variables → New Variable** → `ANTHROPIC_API_KEY` = tu key, si quieres
   la revisión visual de "letras pegadas/cortadas".
6. Abre `https://TU-URL/` en el navegador. Si responde `{"service":"qpc-preflight","status":"ok"}` → ✅ listo.
7. **Mándame esa URL** y yo cableo n8n.

### Alternativa con Railway CLI (sin GitHub)
```bash
npm i -g @railway/cli
railway login
railway init
railway up        # sube esta carpeta y la despliega
railway domain    # genera la URL pública
```

### Alternativa: Render.com
New → Web Service → conecta el repo → Runtime: Docker → Create. Render también da una URL https.

---

## Probar una vez desplegado (opcional)
```bash
# multipart, con un archivo local:
curl -X POST https://TU-URL/preflight \
  -F "files=@arte.pdf" -F "target_width_in=24" -F "target_height_in=18"
```

## Correr local (opcional, para desarrollo)
```bash
pip install -r requirements.txt
uvicorn app:app --reload --port 8000
# o probar sin servidor:
python test_local.py ruta/a/arte.pdf
```

---

## Lo que sigue (lo hago yo cuando me des la URL)
Cableo el flujo **Print Shop - Email Order Classifier** en n8n para que:
1. Junte **todos** los adjuntos del correo y los mande en una sola llamada a `/preflight-json`.
2. Reciba el análisis **por archivo** y arme una respuesta que liste cada uno
   ("Archivo 1: ✅ … / Archivo 2: ⚠️ 36x42, imagen a 95 DPI, sin bleed …").
3. Esto reemplaza los nodos viejos que solo miraban el **primer** archivo y nunca
   medían los PDFs. Quedan resueltos **precisión** y **multi-archivo** al mismo tiempo.

## Notas
- Gmail no recibe adjuntos de más de **25MB** (los manda como link de Drive, que el
  flujo ya lee y también pasa por aquí). Un archivo de 70MB no llega como adjunto de correo.
- Umbrales de DPI por tamaño (ajustables en `app.py`, función `dpi_targets`):
  gran formato ≥24in → mín 100 / ideal 150 · 13–24in → 150/200 · <13in → 200/300.
