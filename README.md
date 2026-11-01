# Estado del acceso

Página de estado de las aplicaciones del Servicio de Dermatología (HUVR): accesibilidad desde España e historial de bloqueos de IP durante los partidos de fútbol.

- `comprobar.py` — comprueba cada 15 minutos (GitHub Actions) si las IPs de cada dirección aparecen en las listas de bloqueo por operador de [hayahora.futbol](https://hayahora.futbol) y si la web responde. Guarda el resultado en `data/`.
- `config.json` — aplicaciones a vigilar (dirección pública y, si hay intermediario, la interna en «origen»).
- `index.html` — la página (GitHub Pages), que lee `data/estado.json` y `data/historial.json`.

Los datos de hayahora.futbol no son oficiales y pueden contener errores.
