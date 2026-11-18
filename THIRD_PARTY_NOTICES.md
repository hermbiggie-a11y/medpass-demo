# Third-party notices

## Upstream

- **MEDPASS** — [AhamedFof/medpass](https://github.com/AhamedFof/medpass), created by the
  Inference team: Ahamed Fofana, Dina Himed, azhao18 and rsqian (GitHub). The upstream repository
  has no licence file and no licence notice; there is no upstream notice text to reproduce. All
  rights are reserved by its authors (see `LICENSE`).
- No third-party repository is bundled (vendored) in this copy.

## Products you install

MEDPASS uses the following software and models. None of them is included in this repository; you
install each from its own source, and each is governed by its own licence and terms, which you
must review and accept before use.

| Component | Used for | Licence |
|---|---|---|
| MongoDB 8 (Community, `mongo:8` Docker image) | Memory: replica set, time-series collection, change streams, transactions | Its own licence, from MongoDB, Inc. |
| OpenClaw | Always-on agent layer woken by the change stream (optional) | Its own licence, from the OpenClaw project |
| Qwen3-VL 30B (`qwen3-vl:30b-a3b-instruct-q4_K_M`, local) | Vision: locating and reading the wristband and pills | The model's own licence, from its publisher |
| Nemotron Nano (`nemotron-3-nano:4b`, local) | Writing read-only MongoDB queries from plain English | The model's own licence, from NVIDIA |
| Tesseract OCR | Second, independent wristband reader | Its own licence, from the Tesseract project |
| OpenCV (`opencv-python-headless`) | Third, independent colour check on pill crops | Its own licence, from the OpenCV project |
| FastAPI, Uvicorn, sse-starlette | API and server-sent events | Each package's own licence |
| PyMongo, Requests, NumPy, pytesseract, pytest | Database driver, HTTP, arrays, OCR binding, tests | Each package's own licence |
| Ollama | Serving the local models | Its own licence, from the Ollama project |
| Piper TTS and the `en_US-amy-medium` voice (optional) | Spoken warning | Their own licences; check the voice's licence before use |
| Docker | Running MongoDB | Its own licence and terms |

## Trademarks

NVIDIA, Grace, Blackwell, DGX and Nemotron are trademarks of NVIDIA Corporation. MongoDB is a
trademark of MongoDB, Inc. Qwen, OpenClaw, Tesseract, OpenCV, FastAPI, Ollama, Piper, Docker and
other product and company names are trademarks of their respective owners. Their use here is for
identification only and does not imply endorsement of Dell Technologies or of this demo by those
owners.
