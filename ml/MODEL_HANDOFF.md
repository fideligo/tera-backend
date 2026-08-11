# JantungSinyal — Handoff Model ke Tim Software

Dokumen ini menjelaskan **apa yang diserahkan** dari sisi ML ke sisi aplikasi, dan **kenapa
formatnya bukan `.tflite`.**

## 1. Kenapa bukan `.tflite`

Model kita adalah **scikit-learn Random Forest** (ansambel pohon keputusan), **bukan** jaringan
saraf TensorFlow/Keras. TFLite hanya bisa mengonversi model TensorFlow/Keras, jadi **Random Forest
tidak bisa langsung diekspor ke `.tflite`.**

Untuk menjalankan model ini di Flutter ada dua jalur yang benar:

| Jalur | Format model | Cara jalan di Flutter | Kapan dipakai |
|---|---|---|---|
| **A. Backend (disarankan)** | `.joblib` | App kirim data ke server FastAPI, inferensi di server | Sesuai arsitektur aplikasi sekarang (app sudah `POST /api/v1/screenings`) |
| **B. On-device (offline)** | `.onnx` atau `trees.json` | `onnxruntime` package, atau evaluasi pohon murni di Dart | Kalau butuh jalan tanpa internet (mis. posyandu) |

`.tflite` hanya relevan kalau model dilatih ulang sebagai neural network — dan itu tidak akan
mengalahkan Random Forest yang sekarang, jadi tidak disarankan.

## 2. Hal terpenting: model butuh pipeline fitur di depannya

Model **tidak** menerima sinyal mentah. Model menerima **10 fitur HRV** yang dihitung dari
interval RR satu jendela (30 denyut). Jadi urutan penuh yang harus jalan (di Dart untuk on-device,
atau di Python untuk backend):

```
Sinyal SCG mentah
  -> band-pass 1-25 Hz -> selubung Hilbert -> deteksi puncak (denyut)
  -> interval RR (detik)
  -> 10 fitur HRV (lihat di bawah)
  -> Random Forest -> probabilitas abnormal -> ambang -> Normal / Perlu Rujuk
```

**Menyerahkan file model saja TIDAK cukup.** Tahap fitur harus ikut diserahkan/diporting.

## 3. Kontrak input model (WAJIB sama persis)

Model mengharapkan vektor **10 angka `float`, urutan tepat seperti ini**, dihitung dari satu
jendela 30 denyut (interval RR dalam detik):

| # | Nama fitur | Definisi |
|---|---|---|
| 0 | `mean_hr` | 60000 / rata-rata(RR_ms) |
| 1 | `mean_rr` | rata-rata(RR_ms) |
| 2 | `sdnn` | standar deviasi(RR_ms) |
| 3 | `rmssd` | akar(rata-rata(selisih RR_ms berurutan^2)) |
| 4 | `rr_cv` | sdnn / mean_rr |
| 5 | `min_rr` | min(RR_ms) |
| 6 | `max_rr` | max(RR_ms) |
| 7 | `pct_long` | proporsi RR > 0.6 detik |
| 8 | `long_brady` | durasi terpanjang RR>0.6s berurutan (detik) |
| 9 | `hr_slope` | kemiringan regresi linear HR-sesaat sepanjang jendela |

(RR_ms = interval RR dalam milidetik = RR_detik x 1000.)

**Output model:** probabilitas kelas abnormal (0..1). Bandingkan dengan `op_threshold` yang
disertakan; di atas ambang = "Perlu Rujuk", di bawah = "Normal". Selalu tampilkan
label **"BUKAN DIAGNOSIS"**.

## 4. Yang diserahkan (isi paket)

- `jantungsinyal_bcg_anomaly_rf.joblib` — model + daftar fitur + ambang (untuk jalur backend).
- `export_model.py` — jalankan sekali untuk menghasilkan:
  - `model.onnx` — untuk on-device via `onnxruntime` Flutter.
  - `model_trees.json` — untuk evaluasi pohon murni di Dart (tanpa dependensi runtime).
  - cetak urutan fitur + nilai ambang.
- Dokumen ini (kontrak fitur + arsitektur).

## 5. Rekomendasi

Untuk purwarupa dan kompetisi: **pakai jalur A (backend).** App sudah mengirim data ke backend,
proposal juga menyebut inferensi di backend, dan tidak perlu porting fitur ke Dart. Serahkan
`.joblib` + kode inferensi Python.

Jalur B (on-device ONNX/Dart) disiapkan bila nanti butuh mode offline — bukan prioritas sekarang.
