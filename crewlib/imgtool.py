"""把图片或 PDF 的一页转成给模型看的 JPEG（工人的 view_image 用）。
用法：python3 imgtool.py <源文件> <输出.jpg> [页码，从 1 开始] [长边像素上限]
用工人那个 Python 跑（workerbin/python3：需安装 PyMuPDF 和 Pillow）。输出一行 JSON。"""
import io
import json
import sys


def main():
    src, out = sys.argv[1], sys.argv[2]
    page = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    side = int(sys.argv[4]) if len(sys.argv) > 4 else 1600
    from PIL import Image
    pages = None
    if src.lower().endswith(".pdf"):
        import fitz
        doc = fitz.open(src)
        pages = doc.page_count
        if not 1 <= page <= pages:
            print(json.dumps({"error": f"页码 {page} 超出范围（共 {pages} 页）"}, ensure_ascii=False))
            return
        pg = doc[page - 1]
        zoom = min(3.0, side / max(pg.rect.width, pg.rect.height))
        pix = pg.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
    else:
        img = Image.open(src)
        n = getattr(img, "n_frames", 1)
        if n > 1:
            pages = n
            img.seek(min(max(page, 1), n) - 1)
    img = img.convert("RGB")
    img.thumbnail((side, side))
    img.save(out, "JPEG", quality=85)
    print(json.dumps({"out": out, "w": img.width, "h": img.height, "pages": pages}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False))
