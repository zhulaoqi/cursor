# AIHub 图像生成接口测试

这是一个独立的最小 Python 项目，用来测试下面两个接口场景：

- 文生图：`prompt` 直接生成图片
- 图像编辑：传入图片 URL 后按提示词编辑图片

## 安装

```bash
cd aihub-image-test
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

然后编辑 `.env`：

```bash
AIHUB_API_KEY=sk-aihub-你的真实密钥
```

## 文生图测试

```bash
python -m aihub_image_test.main text \
  --prompt "Latest Tesla Cybertruck driving through Las Vegas"
```

## 图像编辑测试

```bash
python -m aihub_image_test.main edit \
  --prompt "把背景替换为星空" \
  --image-url "https://example.com/photo.jpg" \
  --mime-type "image/jpeg"
```

## 运行单元测试

单元测试只验证请求参数构造，不会调用真实 API：

```bash
python -m pytest
```

## 退出 Python 虚拟环境

```bash
deactivate
```
