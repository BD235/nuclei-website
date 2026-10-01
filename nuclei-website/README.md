# Nuclei Scan Dashboard — Setup Lengkap

Website custom lokal untuk menjalankan **Nuclei CLI** lewat backend, menyimpan
hasil di SQLite, dan menampilkannya di dashboard. Mengikuti pola:

```
Website custom -> Backend API -> subprocess Nuclei CLI -> JSONL -> Database -> Dashboard
```

Struktur folder:

```
nuclei-website/
├── backend/
│   ├── app.py            # FastAPI: endpoint scan + parsing JSONL
│   ├── profiles.json     # daftar profil scan (bukan flag bebas dari user)
│   ├── requirements.txt
│   ├── Dockerfile
│   └── results/          # dibuat otomatis, isi JSONL/JSON/SARIF per scan
├── frontend/
│   └── index.html        # dashboard single-file, tanpa build step
├── docker-compose.yml
└── README.md
```

## Opsi A — Jalankan dengan Docker (paling mudah)

**Prasyarat:** Docker & Docker Compose sudah terpasang.

1. Masuk ke folder proyek:
   ```bash
   cd nuclei-website
   ```
2. Build dan jalankan semua service (backend, frontend, target uji Juice Shop):
   ```bash
   docker compose up --build
   ```
   Build pertama agak lama karena Nuclei mengunduh template.
3. Buka:
   - Dashboard: `http://localhost:8080`
   - Backend API: `http://localhost:8000/api/profiles` (cek hidup)
   - Target uji (Juice Shop): `http://localhost:3000`
4. Di dashboard, isi target `http://juice-shop:3000` (nama service Docker,
   bukan `localhost`, karena backend berjalan di container terpisah) lalu
   pilih profil `basic`, klik **Start scan**.
5. Tabel scan akan otomatis polling status setiap 2.5 detik. Klik baris scan
   untuk melihat daftar temuan, atau unduh JSONL/JSON/SARIF.

Untuk menghentikan:
```bash
docker compose down
```

## Opsi B — Jalankan manual tanpa Docker

**Prasyarat:**
- Python 3.10+
- Nuclei CLI sudah terpasang dan ada di PATH — cek dengan `nuclei -version`.
  Kalau belum ada, ikuti instalasi resmi:
  https://docs.projectdiscovery.io/tools/nuclei/install
- Target uji berjalan di lokal, misalnya OWASP Juice Shop:
  ```bash
  docker run --rm -p 3000:3000 bkimminich/juice-shop
  ```

Langkah:

1. Masuk ke folder backend dan pasang dependency:
   ```bash
   cd nuclei-website/backend
   python -m venv .venv
   source .venv/bin/activate        # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```
2. Update template Nuclei (sekali saja, atau berkala):
   ```bash
   nuclei -update-templates
   ```
3. Jalankan backend:
   ```bash
   uvicorn app:app --reload --port 8000
   ```
4. Buka `frontend/index.html` langsung di browser (double-click, atau lewat
   `python -m http.server 8080` di folder `frontend/`). Karena `index.html`
   memanggil `http://localhost:8000`, backend harus sudah jalan di port itu.
5. Isi target `http://localhost:3000` (Juice Shop lokal Anda), pilih profil,
   klik **Start scan**.

## Cara kerja tiap bagian

- **`profiles.json`** menetapkan kombinasi tag, severity, rate-limit, dsb.
  Pengguna di frontend hanya memilih *nama profil* (`basic`, `cve`, ...),
  tidak pernah mengirim argumen Nuclei bebas ke backend — ini yang membuat
  endpoint aman dari command injection.
- **`app.py`** menerima `POST /api/scans`, membuat baris di SQLite dengan
  status `queued`, lalu menjalankan `run_scan()` sebagai background task.
  `run_scan()` memanggil `nuclei` lewat `subprocess.run()` dengan command
  tetap yang dibangun dari profil, mengekspor hasil sebagai JSONL, lalu
  mem-parsing tiap baris JSONL ke tabel `findings`.
- **`frontend/index.html`** adalah dashboard statis (tanpa framework) yang
  memanggil API di atas: form untuk membuat scan, tabel scan dengan polling
  status, dan panel temuan per-severity dengan link unduh JSONL/JSON/SARIF.

## Endpoint yang tersedia

| Method | Path | Fungsi |
|---|---|---|
| GET | `/api/profiles` | Daftar profil scan yang tersedia |
| POST | `/api/scans` | Membuat scan baru `{ "target": "...", "profile": "basic" }` |
| GET | `/api/scans` | Daftar semua scan |
| GET | `/api/scans/{scan_id}` | Detail status satu scan |
| GET | `/api/scans/{scan_id}/findings` | Daftar temuan (bisa difilter `?severity=high`) |
| GET | `/api/scans/{scan_id}/results?fmt=jsonl\|json\|sarif` | Unduh file hasil mentah |
| GET | `/api/scans/{scan_id}/report` | Ringkasan markdown sederhana |

## Menambah profil scan baru

Edit `backend/profiles.json`, tambahkan objek baru, contoh untuk template
custom:

```json
"custom-header": {
  "template": "/app/templates/custom-header.yaml",
  "rate_limit": 3,
  "concurrency": 1,
  "bulk_size": 1
}
```

Tidak perlu mengubah `app.py` — `build_nuclei_command()` sudah membaca field
`template`, `tags`, `severity`, dst. secara dinamis dari profil.

## Menambahkan langkah AI analyzer (opsional, langkah berikutnya)

Sesuai rancangan awal, taruh AI **setelah** hasil Nuclei selesai, bukan
sebelum atau menggantikan scan itu sendiri:

1. Tambah endpoint baru, misalnya `POST /api/scans/{scan_id}/analyze`.
2. Endpoint ini membaca baris dari tabel `findings` (bukan file JSONL
   mentah), lalu mengirim field yang sudah disanitasi
   (`template_id`, `name`, `severity`, `matched_at`, `description`) ke API
   Claude untuk diberi penjelasan, prioritas perbaikan, dan kemungkinan
   false positive.
3. Simpan hasilnya ke tabel/file terpisah, misalnya `scan-001-ai.json`,
   supaya bukti asli Nuclei (`scan-001.jsonl`) tidak tercampur dengan
   interpretasi AI.
4. **Jangan** kirim cookie, token, password, JWT, API key, atau response
   HTTP mentah ke AI — kirim hanya field temuan yang sudah diringkas seperti
   di atas.

## Catatan penting

- Backend ini dirancang untuk **memindai target milik Anda sendiri atau yang
  sudah diberi izin eksplisit** (seperti OWASP Juice Shop, yang memang
  dibuat untuk latihan keamanan). Jangan arahkan ke domain pihak ketiga
  tanpa izin.
- `results/` dan `scans.db` disimpan lokal di volume Docker / filesystem
  Anda — tidak ada data yang dikirim ke layanan cloud pihak ketiga, sesuai
  tujuan awal (dashboard sepenuhnya lokal, bukan `-dashboard` milik
  ProjectDiscovery Cloud).
- `-hae/-http-api-endpoint` bawaan Nuclei sengaja **tidak** dipakai di sini
  karena statusnya masih experimental — backend memanggil `nuclei` sebagai
  proses CLI biasa, sesuai rekomendasi di rancangan awal.
