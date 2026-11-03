// Fixed, explicit emulation only. No token access, request interception,
// CAPTCHA handling, random noise or hiding of patched function source.
(config) => {
  "use strict";
  const marker = Symbol.for("ticket-price-monitor.fixed-fingerprint.v1");
  if (globalThis[marker]) return;
  const patchGetter = (key, value) => {
    const proto = Object.getPrototypeOf(navigator);
    const descriptor = Object.getOwnPropertyDescriptor(proto, key);
    // Do not invent unsupported APIs, e.g. deviceMemory on insecure origins.
    if (!descriptor || !descriptor.configurable || typeof descriptor.get !== "function") return;
    const getter = new Proxy(descriptor.get, {
      apply(target, receiver, args) {
        Reflect.apply(target, receiver, args); // Keep native illegal-receiver errors.
        return value;
      }
    });
    Object.defineProperty(proto, key, {...descriptor, get: getter});
  };
  if (config.hardware_concurrency !== undefined) {
    patchGetter("hardwareConcurrency", config.hardware_concurrency);
  }
  if (config.device_memory !== undefined) patchGetter("deviceMemory", config.device_memory);

  if (config.webgl) {
    for (const Constructor of [globalThis.WebGLRenderingContext, globalThis.WebGL2RenderingContext]) {
      if (!Constructor) continue;
      const proto = Constructor.prototype;
      const param = Object.getOwnPropertyDescriptor(proto, "getParameter");
      const extension = Object.getOwnPropertyDescriptor(proto, "getExtension");
      if (!param?.configurable || !extension?.configurable ||
          typeof param.value !== "function" || typeof extension.value !== "function") continue;
      const enabled = new WeakSet();
      Object.defineProperty(proto, "getExtension", {...extension,
        value: new Proxy(extension.value, {
          apply(target, receiver, args) {
            const result = Reflect.apply(target, receiver, args);
            if (result && typeof args[0] === "string" &&
                args[0].toLowerCase() === "webgl_debug_renderer_info") enabled.add(receiver);
            return result;
          }
        })
      });
      Object.defineProperty(proto, "getParameter", {...param,
        value: new Proxy(param.value, {
          apply(target, receiver, args) {
            const original = Reflect.apply(target, receiver, args);
            // Preserve unsupported/lost-context/invalid-enum behavior. Never
            // enable or fabricate an extension the browser did not expose.
            if (typeof original === "string" && enabled.has(receiver)) {
              if (args[0] === 37445) return config.webgl.vendor;
              if (args[0] === 37446) return config.webgl.renderer;
            }
            return original;
          }
        })
      });
    }
  }
  Object.defineProperty(globalThis, marker, {value: true});
}
