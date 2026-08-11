"""
Ekspor model Random Forest JantungSinyal ke format yang bisa dipakai tim software.
Jalankan sekali di mesin yang punya joblib model:

    pip install scikit-learn skl2onnx onnx joblib numpy
    python export_model.py

Menghasilkan:
  - model.onnx        -> untuk on-device di Flutter via package `onnxruntime`
  - model_trees.json  -> untuk evaluasi pohon murni di Dart (tanpa runtime ML)
  - mencetak urutan fitur + ambang operasi
"""
import json, numpy as np, joblib

BUNDLE = "jantungsinyal_bcg_anomaly_rf.joblib"
bundle = joblib.load(BUNDLE)
clf      = bundle["model"]
features = bundle["features"]           # urutan 10 fitur HRV
thr      = bundle.get("op_threshold", 0.5)

print("Fitur (urutan wajib sama di sisi aplikasi):")
for i, f in enumerate(features):
    print(f"  [{i}] {f}")
print(f"Ambang operasi (op_threshold) = {thr:.4f}")
print(f"Jumlah pohon = {len(clf.estimators_)} | jumlah fitur = {clf.n_features_in_}")

# ---------- 1) ONNX (untuk onnxruntime di Flutter) ----------
try:
    from skl2onnx import to_onnx
    onx = to_onnx(clf, np.zeros((1, len(features)), dtype=np.float32),
                  options={id(clf): {"zipmap": False}})
    with open("model.onnx", "wb") as f:
        f.write(onx.SerializeToString())
    print("OK -> model.onnx  (input: float32[1, %d]; output: probabilitas kelas)" % len(features))
except Exception as e:
    print("Lewati ONNX (install skl2onnx onnx):", e)

# ---------- 2) Dump pohon ke JSON (untuk Dart murni) ----------
def dump_tree(t):
    T = t.tree_
    return {
        "children_left":  T.children_left.tolist(),
        "children_right": T.children_right.tolist(),
        "feature":        T.feature.tolist(),
        "threshold":      T.threshold.tolist(),
        # nilai daun -> proporsi kelas [n_normal, n_abnormal] per node
        "value":          T.value.reshape(T.value.shape[0], -1).tolist(),
    }

trees = [dump_tree(est) for est in clf.estimators_]
out = {"features": features, "op_threshold": float(thr),
       "n_classes": int(clf.n_classes_), "trees": trees}
with open("model_trees.json", "w") as f:
    json.dump(out, f)
print(f"OK -> model_trees.json  ({len(trees)} pohon)")
print("\nCatatan Dart: untuk tiap pohon, telusuri node dari 0; jika feature[node] == -2 -> daun,"
      "\nprobabilitas = value[node] dinormalisasi; rata-ratakan probabilitas semua pohon.")
