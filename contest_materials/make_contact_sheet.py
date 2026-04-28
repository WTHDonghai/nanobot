from pathlib import Path
import sys

from PIL import Image, ImageDraw, ImageFont


def make_sheet(src_dir: Path, out_path: Path, cols: int = 3):
    pages = sorted(src_dir.glob("page-*.png"))
    if not pages:
        raise SystemExit(f"no page images in {src_dir}")

    thumb_w = 360
    label_h = 30
    padding = 18
    thumbs = []
    for page in pages:
        image = Image.open(page).convert("RGB")
        ratio = thumb_w / image.width
        thumb_h = int(image.height * ratio)
        image = image.resize((thumb_w, thumb_h), Image.Resampling.LANCZOS)
        thumbs.append((page.name, image))

    rows = (len(thumbs) + cols - 1) // cols
    cell_h = max(img.height for _, img in thumbs) + label_h + padding
    sheet = Image.new("RGB", (cols * (thumb_w + padding) + padding, rows * cell_h + padding), "white")
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("Arial.ttf", 16)
    except Exception:
        font = ImageFont.load_default()

    for idx, (name, img) in enumerate(thumbs):
        row = idx // cols
        col = idx % cols
        x = padding + col * (thumb_w + padding)
        y = padding + row * cell_h
        draw.text((x, y), name, fill=(40, 50, 60), font=font)
        sheet.paste(img, (x, y + label_h))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path)
    print(out_path)


if __name__ == "__main__":
    make_sheet(Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3]) if len(sys.argv) > 3 else 3)
