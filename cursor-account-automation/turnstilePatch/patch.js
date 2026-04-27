// turnstilePatch - 修复 CDP MouseEvent.screenX/screenY 坐标泄露
//
// CDP dispatchMouseEvent 的 screenX/screenY 始终为 0
// Cloudflare Turnstile 检测这个特征判断自动化
// 用 get 拦截器覆盖原型属性，对值为 0 的返回随机合理值

(function () {
  const randomInt = (min, max) =>
    Math.floor(Math.random() * (max - min + 1)) + min;

  const patchProp = (proto, prop, min, max) => {
    const orig = Object.getOwnPropertyDescriptor(proto, prop);
    Object.defineProperty(proto, prop, {
      configurable: true,
      enumerable: true,
      get: function () {
        const real = orig && orig.get ? orig.get.call(this) : 0;
        return real === 0 ? randomInt(min, max) : real;
      },
    });
  };

  patchProp(MouseEvent.prototype, "screenX", 300, 1800);
  patchProp(MouseEvent.prototype, "screenY", 200, 900);
})();
