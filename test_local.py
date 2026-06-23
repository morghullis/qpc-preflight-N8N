"""Prueba local: corre el preflight sobre archivos en ./samples sin levantar servidor.
Uso:  python test_local.py archivo1.pdf archivo2.png
"""
import sys, json, app
target = None  # pon {"w":24,"h":18} si quieres evaluar DPI de imagenes a un tamano
for path in sys.argv[1:]:
    data = open(path, "rb").read()
    r = app.analyze_one(path.split("/")[-1], data, target)
    print(json.dumps({k:v for k,v in r.items() if k!="trace"}, ensure_ascii=False, indent=2))
