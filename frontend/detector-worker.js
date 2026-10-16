/* The detection model, in its own thread.
 *
 * station.js owns the camera, the preview, the alarm and the uploads; this
 * file owns TensorFlow.js and COCO-SSD. They talk by message, so a model that
 * is slow to download, throws while loading, or takes a while per frame can
 * hold up detection but never the camera or the live view.
 *
 *   in:  {type: "load", tfjs, coco}
 *        {type: "detect", id, bitmap, maxBoxes, minScore}
 *   out: {type: "ready"} | {type: "error", message}
 *        {type: "result", id, preds} | {type: "failed", id, message}
 */
let model = null;

/* Download progress. TF.js fetches model.json and then each weight shard
 * through the global fetch, so wrapping it counts the bytes as they arrive.
 * The total is only what the servers announce, and it grows as each file
 * starts, so the figure is an estimate -- good enough for a bar. */
let loadedBytes = 0;
let totalBytes = 0;
let lastReport = 0;

function report(force) {
  const now = Date.now();
  if (!force && now - lastReport < 100) return;
  lastReport = now;
  self.postMessage({
    type: "progress",
    loaded: loadedBytes,
    total: totalBytes,
  });
}

const realFetch = self.fetch.bind(self);
self.fetch = async (...args) => {
  const res = await realFetch(...args);
  if (!res.body) return res;
  totalBytes += Number(res.headers.get("content-length")) || 0;
  const reader = res.body.getReader();
  const counted = new ReadableStream({
    async pull(controller) {
      const { done, value } = await reader.read();
      if (done) {
        controller.close();
        report(true);
        return;
      }
      loadedBytes += value.length;
      report(false);
      controller.enqueue(value);
    },
    cancel: (why) => reader.cancel(why),
  });
  return new Response(counted, {
    status: res.status,
    statusText: res.statusText,
    headers: res.headers,
  });
};

async function load({ tfjs, coco }) {
  importScripts(tfjs, coco);
  await tf.ready();
  // lite_mobilenet_v2 is the small one: the closest thing to the agent's
  // MobileNet-SSD, and the only sensible choice on a phone.
  model = await cocoSsd.load({ base: "lite_mobilenet_v2" });
}

self.onmessage = async (e) => {
  const m = e.data;
  if (m.type === "load") {
    try {
      await load(m);
      self.postMessage({ type: "ready", backend: tf.getBackend() });
    } catch (err) {
      self.postMessage({ type: "error", message: err?.message || String(err) });
    }
  } else if (m.type === "detect") {
    try {
      if (!model) throw new Error("the model is not loaded");
      const preds = await model.detect(m.bitmap, m.maxBoxes, m.minScore);
      self.postMessage({
        type: "result",
        id: m.id,
        preds: preds.map((p) => ({
          class: p.class,
          score: p.score,
          bbox: p.bbox,
        })),
      });
    } catch (err) {
      self.postMessage({
        type: "failed",
        id: m.id,
        message: err?.message || String(err),
      });
    } finally {
      m.bitmap?.close?.();
    }
  }
};
