# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.10,<2", "av>=14", "pillow>=10.1", "pillow-heif>=0.16", "pydantic>=2.7"]
# ///
"""Regenera las figuras y las respuestas JSON de docs/formatos.md y del README.

    uv run --script docs/generar_ejemplos.py

1. Crea los medios de prueba en docs/formatos/salidas/ (no se suben; salen iguales cada vez):
   - ciudad.mp4: 640×360, 30 fps, 20 s = 600 fotogramas. Cada fotograma lleva escritos su
     número, su tiempo y su %, para comprobar a simple vista que las etiquetas y el JSON
     dicen la verdad. Al fondo se desplaza una ciudad; la primera mitad de cada segundo
     está desenfocada (como un vídeo movido) y los 3 últimos segundos son un plano fijo.
   - fotos/: 10 imágenes (JPEG y PNG, una vertical, una girada por EXIF como las de un
     móvil, dos movidas y una casi repetida).
   - movil.mp4: 3 s grabados «en vertical» como un móvil: la imagen guardada de lado y un
     giro de 90° en la matriz de visualización.
   - planos.mp4: 640×360, 30 fps, 17 s con 6 planos y transiciones conocidas: corte a 3 s,
     encadenado de 1 s, fundido a negro y vuelta, corte, encadenado de 0,5 s y fundido final.
     Cada fotograma lleva su número, su tiempo y su plano.
2. Llama a las herramientas de server.py con run_tool(), igual que un asistente por MCP.
   Las respuestas se guardan en docs/formatos/json/, con las rutas cambiadas por
   /Users/ana/ejemplos/ (son rutas de ejemplo).
3. Monta con esas salidas reales las figuras docs/formatos/fig_*.png. De los vídeos (mosaico,
   animática, presentación) enseña fotogramas sacados del archivo que se escribió; un mosaico
   pequeño en WebP animado se copia a docs/formatos/mosaico.webp para el README.
"""
import colorsys
import importlib.util
import json
import math
import os
import random
import shutil
from pathlib import Path

import av
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "formatos"
WORK = OUT / "salidas"
FAKE = "/Users/ana/ejemplos"                 # prefijo de las rutas en los JSON publicados
W, H, FPS, SECONDS, STILL_FROM, SCROLL = 640, 360, 30, 20, 17.0, 48
N = FPS * SECONDS


# ----------------------------------------------------------------------------- fuentes
def font(size: int, bold: bool = False, mono: bool = False) -> ImageFont.FreeTypeFont:
    if mono:
        cands = [("/System/Library/Fonts/Menlo.ttc", 1 if bold else 0),
                 ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono" + ("-Bold" if bold else "") + ".ttf", 0)]
    else:
        cands = [("/System/Library/Fonts/HelveticaNeue.ttc", 1 if bold else 0),
                 ("/usr/share/fonts/truetype/dejavu/DejaVuSans" + ("-Bold" if bold else "") + ".ttf", 0)]
    for path, index in cands:
        try:
            return ImageFont.truetype(path, size, index=index)
        except OSError:
            pass
    return ImageFont.load_default(size)


# ----------------------------------------------------------------------------- 1. medios
def _city():
    rng, out, x = random.Random(7), [], -20
    while x < W + SCROLL * SECONDS + 40:
        w, h = rng.randint(30, 86), rng.randint(64, 168)
        wins = [(wx, wy) for wy in range(H - h + 12, H - 22, 14) for wx in range(x + 7, x + w - 8, 11)
                if rng.random() < 0.38]
        out.append((x, w, h, rng.randint(0, 2), wins))
        x += w + rng.randint(0, 12)
    return out


CITY = _city()


def _mix(a, b, f):
    return tuple(round(a[k] + (b[k] - a[k]) * f) for k in range(3))


def frame_image(i: int) -> Image.Image:
    t = i / FPS
    still = t >= STILL_FROM
    tc = STILL_FROM if still else t
    f = tc / SECONDS
    top, bottom = _mix((37, 99, 235), (234, 88, 12), f), _mix((191, 219, 254), (254, 215, 170), f)
    im = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(im)
    for y in range(H):
        d.line((0, y, W, y), fill=_mix(top, bottom, y / H))
    off = round(tc * SCROLL)
    tones = [(30, 41, 59), (51, 65, 85), (15, 23, 42)]
    for x, w, h, tone, wins in CITY:
        if x - off > W or x + w - off < 0:
            continue
        d.rectangle((x - off, H - h, x - off + w, H), fill=tones[tone])
        for wx, wy in wins:
            d.rectangle((wx - off, wy, wx - off + 4, wy + 6), fill=(253, 224, 71))
    card = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    c = ImageDraw.Draw(card)
    c.rounded_rectangle((112, 22, W - 112, 170), radius=18, fill=(12, 12, 16, 230))
    c.text((W // 2, 80), f"#{i}", font=font(82, bold=True, mono=True), fill=(255, 255, 255), anchor="mm")
    c.text((W // 2, 142), f"{t:.2f} s · {100 * i / (N - 1):.1f} %", font=font(30, mono=True),
           fill=(253, 224, 71), anchor="mm")
    if still:
        c.rounded_rectangle((W // 2 - 84, 184, W // 2 + 84, 218), radius=17, fill=(255, 255, 255, 240))
        c.text((W // 2, 201), "PLANO FIJO", font=font(19, bold=True), fill=(12, 12, 16), anchor="mm")
    c.rounded_rectangle((24, H - 24, W - 24, H - 18), radius=3, fill=(255, 255, 255, 200))
    xm = 24 + (W - 48) * i / (N - 1)
    c.ellipse((xm - 9, H - 30, xm + 9, H - 12), fill=(255, 255, 255, 255), outline=(12, 12, 16, 255), width=3)
    im = Image.alpha_composite(im.convert("RGBA"), card).convert("RGB")
    if not still and (t % 1.0) < 0.5:
        im = im.filter(ImageFilter.GaussianBlur(4))
    return im


def make_video(path: Path) -> None:
    with av.open(str(path), "w") as c:
        st = c.add_stream("mpeg4", rate=FPS)
        st.width, st.height, st.pix_fmt, st.bit_rate = W, H, "yuv420p", 3_000_000
        for i in range(N):
            for pkt in st.encode(av.VideoFrame.from_image(frame_image(i))):
                c.mux(pkt)
        for pkt in st.encode():
            c.mux(pkt)


def make_phone_video(path: Path) -> None:
    """Portrait scene stored sideways plus a display matrix, the way a phone records it."""
    big, small = font(64, bold=True), font(28)
    with av.open(str(path), "w") as c:
        st = c.add_stream("mpeg4", rate=FPS)
        st.width, st.height, st.pix_fmt, st.bit_rate = 640, 360, "yuv420p", 2_000_000
        st.set_display_rotation(-90)                    # show it turned 90° clockwise
        for i in range(90):
            im = Image.new("RGB", (360, 640))
            d = ImageDraw.Draw(im)
            for y in range(640):
                d.line((0, y, 360, y), fill=_mix((37, 99, 235), (234, 88, 12), y / 640))
            bx = 60 + (240 * i / 89)
            d.ellipse((bx - 34, 420, bx + 34, 488), fill=(253, 224, 71))
            d.polygon([(180, 60), (115, 150), (245, 150)], fill=(255, 255, 255))
            d.rectangle((160, 150, 200, 230), fill=(255, 255, 255))
            d.text((180, 300), "ARRIBA", font=big, fill="white", anchor="mm")
            d.text((180, 360), "vídeo de móvil", font=small, fill="white", anchor="mm")
            d.rounded_rectangle((90, 540, 270, 610), radius=14, fill=(12, 12, 16))
            d.text((180, 575), f"#{i}", font=font(44, bold=True, mono=True), fill=(253, 224, 71), anchor="mm")
            for pkt in st.encode(av.VideoFrame.from_image(im.transpose(Image.Transpose.ROTATE_90))):
                c.mux(pkt)
        for pkt in st.encode():
            c.mux(pkt)


# Planos de planos.mp4 y transiciones entre ellos: ("cut",), ("dissolve", s) o ("fade", salida, negro, entrada)
SHOTS = [("ciudad", 3.0), ("cut",), ("mar", 2.5), ("dissolve", 1.0), ("sala", 3.0), ("fade", 0.5, 0.5, 0.5),
         ("bosque", 3.0), ("cut",), ("noche", 2.0), ("dissolve", 0.5), ("fin", 2.5)]
END_FADE = 1.0


def shot_scene(kind: str, t: float) -> Image.Image:
    im = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(im)
    if kind == "ciudad":                                     # la ciudad del otro vídeo, de día
        for y in range(H):
            d.line((0, y, W, y), fill=_mix((37, 99, 235), (191, 219, 254), y / H))
        off = round(t * SCROLL)
        for x, w, h, tone, wins in CITY:
            if -w <= x - off <= W:
                d.rectangle((x - off, H - h, x - off + w, H), fill=[(30, 41, 59), (51, 65, 85), (15, 23, 42)][tone])
    elif kind == "mar":
        for y in range(H):
            d.line((0, y, W, y), fill=(10, round(60 + 100 * y / H), round(120 + 120 * y / H)))
        for k in range(9):
            yy = H * (0.38 + 0.07 * k)
            d.line([(x, yy + 7 * math.sin(x / 28 + t * 3 + k)) for x in range(0, W + 10, 10)],
                   fill=(200, 230, 255), width=3)
    elif kind == "sala":
        d.rectangle((0, 0, W, H), fill=(150, 90, 50))
        d.rectangle((0, H * 0.7, W, H), fill=(90, 50, 30))
        d.rectangle((W * 0.1, H * 0.15, W * 0.35, H * 0.5), fill=(230, 220, 180))
        bx = W * (0.25 + 0.5 * t / 3)
        d.ellipse((bx - 30, H * 0.55, bx + 30, H * 0.55 + 60), fill=(200, 30, 30))
    elif kind == "bosque":                                   # paneo rápido
        d.rectangle((0, 0, W, H), fill=(30, 80, 40))
        rng = random.Random(5)
        for _ in range(60):
            x = (rng.randint(0, 3 * W) - round(t * W * 0.5)) % (3 * W) - W
            d.rectangle((x, 0, x + rng.randint(8, 34), H), fill=(rng.randint(60, 110), rng.randint(40, 70), 20))
    elif kind == "noche":
        d.rectangle((0, 0, W, H), fill=(8, 10, 25))
        rng = random.Random(9)
        for _ in range(60):
            d.point((rng.randint(0, W), rng.randint(0, H // 2)), fill=(200, 200, 220))
        lx = W * (0.3 + 0.4 * math.sin(t))
        d.ellipse((lx - 34, H * 0.25, lx + 34, H * 0.25 + 68), fill=(255, 230, 150))
    else:                                                    # rótulo final
        d.text((W / 2, H / 2), "FIN", font=font(96, bold=True), fill=(255, 255, 255), anchor="mm")
    return im


def _stamp(im: Image.Image, i: int, shot: int) -> Image.Image:
    card = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    c = ImageDraw.Draw(card)
    c.rounded_rectangle((16, H - 84, 300, H - 16), radius=12, fill=(12, 12, 16, 220))   # abajo: la etiqueta va arriba
    c.text((30, H - 76), f"#{i} · {i / FPS:.2f} s", font=font(26, bold=True, mono=True), fill=(255, 255, 255))
    c.text((30, H - 42), f"PLANO {shot}", font=font(18, bold=True), fill=(253, 224, 71))
    return Image.alpha_composite(im.convert("RGBA"), card).convert("RGB")


def shots_timeline() -> list:
    """Una entrada por fotograma: qué plano o mezcla de planos se ve, y con qué peso."""
    scenes, trans = SHOTS[0::2], SHOTS[1::2]
    frames: list = []
    t0 = 0.0                                                 # tiempo de la escena al empezar sus fotogramas
    for k, (kind, dur) in enumerate(scenes):
        own = round(dur * FPS)
        frames += [[(k, t0 + j / FPS, 1.0)] for j in range(own)]
        end, t0 = t0 + own / FPS, 0.0
        tr = trans[k] if k < len(trans) else None
        if tr and tr[0] == "dissolve":
            m = round(tr[1] * FPS)
            frames += [[(k, end + j / FPS, 1 - (j + 0.5) / m), (k + 1, j / FPS, (j + 0.5) / m)] for j in range(m)]
            t0 = m / FPS
        elif tr and tr[0] == "fade":
            o, b, n = (round(x * FPS) for x in tr[1:])
            frames += [[(k, end + j / FPS, 1 - (j + 1) / o)] for j in range(o)] + [[]] * b
            frames += [[(k + 1, j / FPS, (j + 1) / n)] for j in range(n)]
            t0 = n / FPS
    n_end = round(END_FADE * FPS)
    for j in range(n_end):
        frames[-n_end + j] = [(k, t, w * (1 - (j + 1) / n_end)) for k, t, w in frames[-n_end + j]]
    return frames


def make_shots_video(path: Path) -> None:
    kinds = [s[0] for s in SHOTS[0::2]]
    with av.open(str(path), "w") as c:
        st = c.add_stream("mpeg4", rate=FPS)
        st.width, st.height, st.pix_fmt, st.bit_rate = W, H, "yuv420p", 3_000_000
        black = Image.new("RGB", (W, H))
        for i, parts in enumerate(shots_timeline()):
            pics = [(_stamp(shot_scene(kinds[k], max(0.0, t)), i, k + 1), w) for k, t, w in parts]
            if not pics:                                      # negro entre dos fundidos
                im = black
            elif len(pics) == 1:                              # un plano, quizá fundiéndose con el negro
                im = Image.blend(black, pics[0][0], pics[0][1])
            else:                                             # encadenado: mezcla de los dos
                im = Image.blend(pics[0][0], pics[1][0], pics[1][1])
            for pkt in st.encode(av.VideoFrame.from_image(im)):
                c.mux(pkt)
        for pkt in st.encode():
            c.mux(pkt)


def make_photos(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    big = font(96, bold=True)

    def scene(k: int, hue: float, w: int = 1200, h: int = 800) -> Image.Image:
        sky = tuple(round(255 * v) for v in colorsys.hsv_to_rgb(hue, 0.35, 0.95))
        im = Image.new("RGB", (w, h), sky)
        d = ImageDraw.Draw(im)
        rng = random.Random(k)
        for j in range(9):                                     # montañas
            x = rng.randint(-200, w)
            d.polygon([(x, h), (x + 260, h * 0.35 + rng.randint(0, 200)), (x + 520, h)],
                      fill=tuple(round(255 * v) for v in colorsys.hsv_to_rgb(hue, 0.5, 0.35 + 0.05 * j)))
        d.ellipse((w * 0.72, h * 0.12, w * 0.72 + 120, h * 0.12 + 120), fill=(255, 244, 214))
        d.rounded_rectangle((w / 2 - 230, h / 2 - 85, w / 2 + 230, h / 2 + 85), radius=26, fill=(12, 12, 16))
        d.text((w / 2, h / 2), f"FOTO {k}", font=big, fill=(255, 255, 255), anchor="mm")
        return im

    for k in range(1, 9):
        im = scene(k, k / 9)
        if k in (3, 6):
            im = im.filter(ImageFilter.GaussianBlur(10))       # movidas
        im.save(folder / (f"foto_{k:02d}.png" if k % 4 == 0 else f"foto_{k:02d}.jpg"), quality=90)
    scene(5, 5 / 9).save(folder / "foto_05b.jpg", quality=88)   # casi igual que la 5
    vertical = scene(9, 0.95, 800, 1200).transpose(Image.Transpose.ROTATE_90)
    exif = Image.Exif()
    exif[0x0112] = 6                                          # el móvil la guardó girada
    vertical.save(folder / "foto_09_movil.jpg", exif=exif, quality=90)


# ----------------------------------------------------------------------------- 2. herramientas
def load_server():
    spec = importlib.util.spec_from_file_location("server", ROOT / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SRV = None


def run(name: str, tool: str, args: dict) -> dict:
    """Llama a una herramienta y guarda la respuesta (con rutas de ejemplo) en json/<name>.json."""
    data, _ = SRV.run_tool(tool, args)
    text = json.dumps(data, ensure_ascii=False, indent=1).replace(str(WORK.resolve()), FAKE)
    (OUT / "json").mkdir(exist_ok=True)
    (OUT / "json" / f"{name}.json").write_text(text + "\n", encoding="utf-8")
    print(f"  {name}: {tool}")
    return data


# ----------------------------------------------------------------------------- 3. figuras
INK, MUTED, RULE, PAPER, CODE_BG, ACCENT = (24, 24, 27), (82, 82, 91), (228, 228, 231), (255, 255, 255), \
    (244, 244, 245), (37, 99, 235)
FIG_W, PAD, GAP = 1440, 44, 36
F_TITLE, F_SUB, F_HEAD = font(36, bold=True), font(21), font(24, bold=True)
F_TEXT, F_CODE, F_BADGE = font(21), font(18, mono=True), font(19, bold=True)


def wrap(d, text, f, width):
    out = []
    for para in text.split("\n"):
        line = ""
        for word in para.split():
            cand = f"{line} {word}".strip()
            if line and d.textlength(cand, font=f) > width:
                out.append(line)
                line = word
            else:
                line = cand
        out.append(line)
    return out


def badge(d, cx, cy, n, r=17):
    d.ellipse((cx - r - 3, cy - r - 3, cx + r + 3, cy + r + 3), fill=PAPER)
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=ACCENT)
    d.text((cx, cy + 1), str(n), font=F_BADGE, fill=PAPER, anchor="mm")


def fit(path, width):
    im = Image.open(path).convert("RGB")
    return im if im.width <= width else im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)


def heading(d, title, subtitle):
    d.text((PAD, PAD), title, font=F_TITLE, fill=INK)
    y = PAD + 50
    for ln in wrap(d, subtitle, F_SUB, FIG_W - 2 * PAD):
        d.text((PAD, y), ln, font=F_SUB, fill=MUTED)
        y += 30
    return y + 22


def side_text(d, x, y, width, code, text, draw=True):
    lh, h = 27, 0
    if code:
        box = 14 + lh * len(code)
        if draw:
            d.rounded_rectangle((x, y, x + width, y + box), radius=8, fill=CODE_BG)
            for n, ln in enumerate(code):
                d.text((x + 12, y + 8 + n * lh), ln, font=F_CODE, fill=INK)
        h += box + 14
    lines = wrap(d, text, F_TEXT, width)
    if draw:
        for n, ln in enumerate(lines):
            d.text((x, y + h + n * 30), ln, font=F_TEXT, fill=MUTED)
    return h + 30 * len(lines)


def save_fig(fig: Image.Image, out: Path):
    fig.quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.FLOYDSTEINBERG).save(
        out, optimize=True)
    print(f"  figura {out.name}  {fig.width}×{fig.height}  {out.stat().st_size // 1024} KB")


def rows_figure(title, subtitle, rows, out, img_w=860):
    """Una fila por variante: número y título, la salida real a la izquierda y, a la derecha,
    la llamada que la produce y qué es."""
    txt_w = FIG_W - 2 * PAD - img_w - GAP
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    imgs = [fit(p, img_w) for _, p, _, _ in rows]
    heights = [44 + max(im.height, side_text(probe, 0, 0, txt_w, code, text, False))
               for im, (_, _, code, text) in zip(imgs, rows)]
    head_h = heading(probe, title, subtitle)
    fig = Image.new("RGB", (FIG_W, head_h + sum(heights) + GAP * (len(rows) - 1) + PAD), PAPER)
    d = ImageDraw.Draw(fig)
    y = heading(d, title, subtitle)
    for n, (im, (head, _, code, text), h) in enumerate(zip(imgs, rows, heights), 1):
        badge(d, PAD + 17, y + 15, n)
        d.text((PAD + 46, y + 15), head, font=F_HEAD, fill=INK, anchor="lm")
        fig.paste(im, (PAD, y + 44))
        d.rectangle((PAD - 1, y + 43, PAD + im.width, y + 44 + im.height), outline=RULE)
        side_text(d, PAD + img_w + GAP, y + 44, txt_w, code, text)
        y += h + GAP
        if n < len(rows):
            d.line((PAD, y - GAP // 2, FIG_W - PAD, y - GAP // 2), fill=RULE, width=2)
    save_fig(fig, out)


def grid_figure(title, subtitle, items, out, cols):
    cell = (FIG_W - 2 * PAD - GAP * (cols - 1)) // cols
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    imgs = [fit(p, cell) for _, p, _, _ in items]
    rows = [list(range(k, min(k + cols, len(items)))) for k in range(0, len(items), cols)]
    row_h = [max(44 + imgs[k].height + 14 + side_text(probe, 0, 0, cell, items[k][2], items[k][3], False)
                 for k in r) for r in rows]
    head_h = heading(probe, title, subtitle)
    fig = Image.new("RGB", (FIG_W, head_h + sum(row_h) + GAP * (len(rows) - 1) + PAD), PAPER)
    d = ImageDraw.Draw(fig)
    y = heading(d, title, subtitle)
    for r, h in zip(rows, row_h):
        for j, k in enumerate(r):
            head, _, code, text = items[k]
            x = PAD + j * (cell + GAP)
            badge(d, x + 17, y + 15, k + 1)
            d.text((x + 46, y + 15), head, font=F_HEAD, fill=INK, anchor="lm")
            fig.paste(imgs[k], (x, y + 44))
            d.rectangle((x - 1, y + 43, x + imgs[k].width, y + 44 + imgs[k].height), outline=RULE)
            side_text(d, x, y + 44 + imgs[k].height + 14, cell, code, text)
        y += h + GAP
    save_fig(fig, out)


def anatomy_figure(sheet: Path, data: dict, out: Path):
    """La hoja real con sus partes numeradas y la leyenda debajo."""
    src = Image.open(sheet).convert("RGB")
    lay = data["layout"]
    pw, ph = lay["picture_size"]
    # geometría con las mismas fórmulas que server.py (Look.set_sizes y _plan)
    text_size = max(18, round(src.width / 64))
    title_size = round(text_size * 1.3)
    pad = max(6, round(text_size * 0.5))
    head_h = pad * 2 + round(title_size * 1.2) + pad // 2 + round(text_size * 1.3)
    foot_h = pad * 2 + round(text_size * 1.3)
    label = max(12, round(pw / 22))
    band = label + max(8, label // 2)
    k = (FIG_W - 2 * PAD) / src.width
    im = src.resize((FIG_W - 2 * PAD, round(src.height * k)), Image.LANCZOS)
    legend = [
        ("Cabecera", "Datos del vídeo: nombre, tamaño, fps, duración, nº de fotogramas y códec.",
         ['"layout.title", "subtitle"', '"header": false la quita']),
        ("Etiqueta", "Número de viñeta, segundos y nº de fotograma; en el primero y el último, además, "
         "«primer / último fotograma».", ['"layout.label"', '"fields" o "template"']),
        ("Viñetas", f"{len(data['panels'])} fotogramas en orden de tiempo, por filas. La viñeta n es el "
         "elemento n de «panels» en el JSON.", ['"select": {"count": 12}', '"layout": {"columns": 4}']),
        ("Pie", "Cómo se eligieron: modo, rango, fotogramas medidos y nitidez mín / mediana / máx.",
         ['"footer": false lo quita']),
    ]
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    cell = (FIG_W - 2 * PAD - GAP * 3) // 4
    leg_h = max(40 + side_text(probe, 0, 0, cell, code, text, False) for _, text, code in legend)
    title = "La respuesta más simple: una hoja de contactos"
    sub = ('make_storyboard con solo {"input": {"video": ".../ciudad.mp4"}}: los 12 fotogramas más nítidos, '
           f"uno por tramo, en 4 columnas y tema oscuro. La imagen real mide {src.width}×{src.height} px.")
    head = heading(probe, title, sub)
    fig = Image.new("RGB", (FIG_W, head + im.height + 30 + leg_h + PAD), PAPER)
    d = ImageDraw.Draw(fig)
    y0 = heading(d, title, sub)
    fig.paste(im, (PAD, y0))
    for n, yy in enumerate((head_h / 2, head_h + band / 2, head_h + ph * 0.6, src.height - foot_h / 2), 1):
        cy = y0 + round(yy * k)                 # badges in the left margin, with a short line into the part
        d.line((PAD // 2, cy, PAD + 26, cy), fill=ACCENT, width=3)
        badge(d, PAD // 2, cy, n, r=15)
    y = y0 + im.height + 30
    for n, (name, text, code) in enumerate(legend, 1):
        x = PAD + (n - 1) * (cell + GAP)
        badge(d, x + 17, y + 15, n)
        d.text((x + 46, y + 15), name, font=F_HEAD, fill=INK, anchor="lm")
        side_text(d, x, y + 44, cell, code, text)
    save_fig(fig, out)


# ----------------------------------------------------------------------------- main
def main() -> None:
    global SRV
    WORK.mkdir(parents=True, exist_ok=True)
    video, photos, phone = WORK / "ciudad.mp4", WORK / "fotos", WORK / "movil.mp4"
    if not video.exists():
        print("creando ciudad.mp4 …")
        make_video(video)
    if not phone.exists():
        make_phone_video(phone)
    if not photos.exists():
        make_photos(photos)
    SRV = load_server()
    V, F, O = str(video), str(photos), str(WORK)
    out = lambda name, **kw: {"dir": O, "name": name, "preview": False, **kw}   # noqa: E731
    print("llamadas:")
    run("media_info_video", "media_info", {"input": {"video": V}})
    run("media_info_fotos", "media_info", {"input": {"folder": F}})
    minimo = run("minimo", "make_storyboard", {"input": {"video": V}, "output": out("minimo")})

    # plantillas
    notas = [{"shot": "PG", "action": "Amanece sobre la ciudad.", "sound": "Tráfico lejano"},
             {"shot": "PM", "camera": "Travelling lateral", "action": "La luz cambia.", "transition": "Corte"},
             {"shot": "PP", "dialogue": "VOZ EN OFF: Cada día igual.", "duration_s": 2.5}]
    sel3 = {"at_s": [2.5, 9.5, 15.5]}
    run("plantilla_grid", "make_storyboard", {"input": {"video": V}, "select": {"count": 6},
                                              "layout": {"panel_width": 320, "columns": 3},
                                              "output": out("plantilla_grid")})
    run("plantilla_storyboard", "make_storyboard", {"input": {"video": V}, "select": sel3, "panels": notas,
                                                    "layout": {"template": "storyboard", "panel_width": 320},
                                                    "output": out("plantilla_storyboard")})
    run("plantilla_list", "make_storyboard", {"input": {"video": V}, "select": {"at_s": [2.5, 9.5]},
                                              "panels": notas, "layout": {"template": "list", "panel_width": 320},
                                              "output": out("plantilla_list")})
    run("plantilla_strip", "make_storyboard", {"input": {"video": V}, "select": {"count": 4}, "panels": notas,
                                               "layout": {"template": "strip", "panel_width": 280},
                                               "output": out("plantilla_strip")})
    # temas
    base = {"input": {"video": V}, "select": sel3, "panels": notas, "layout": {"template": "storyboard",
                                                                                "panel_width": 320}}
    run("tema_oscuro", "make_storyboard", {**base, "output": out("tema_oscuro")})
    run("tema_claro", "make_storyboard", {**base, "style": {"theme": "light"}, "output": out("tema_claro")})
    run("tema_propio", "make_storyboard", {**base, "style": {
        "background": "#1e293b", "text": "#f8fafc", "text_secondary": "#94a3b8", "accent": "#f472b6",
        "panel_border": "#334155", "notes_background": "#0f172a", "notes_text": "#e2e8f0",
        "font": "Georgia", "font_bold": "Georgia Bold"}, "output": out("tema_propio")})
    # selección
    sel = {"input": {"video": V}, "layout": {"panel_width": 256, "columns": 6,
                                             "label": {"fields": ["number", "frame", "sharpness"]}}}
    run("sel_nitido", "make_storyboard", {**sel, "select": {"count": 6}, "output": out("sel_nitido")})
    run("sel_centro", "make_storyboard", {**sel, "select": {"count": 6, "mode": "even"},
                                          "output": out("sel_centro")})
    run("sel_exacto", "make_storyboard", {**sel, "select": {"step_pct": 20}, "output": out("sel_exacto")})
    run("sel_exacto_snap", "make_storyboard", {**sel, "select": {"step_pct": 20, "snap_s": 0.5},
                                               "output": out("sel_exacto_snap")})
    run("filtros_sin", "make_storyboard", {**sel, "select": {"count": 10, "mode": "even"},
                                           "layout": {**sel["layout"], "columns": 5},
                                           "output": out("filtros_sin")})
    run("filtros_con", "make_storyboard", {**sel, "select": {"count": 10, "mode": "even", "drop_blurriest_pct": 50,
                                                             "min_difference": 6},
                                           "layout": {**sel["layout"], "columns": 5},
                                           "output": out("filtros_con")})
    # etiquetas y numeración
    lab = {"input": {"video": V}, "select": {"at_pct": [0, 100]}}
    lw = {"panel_width": 400, "columns": 2}
    run("etiqueta_defecto", "make_storyboard", {**lab, "layout": lw, "output": out("etiqueta_defecto")})
    run("etiqueta_campos", "make_storyboard", {**lab, "layout": {**lw, "label": {
        "fields": ["number", "timecode", "pct"], "position": "bottom"}}, "output": out("etiqueta_campos")})
    run("etiqueta_plantilla", "make_storyboard", {**lab, "layout": {**lw, "numbering": {"format": "SH{n:03d}"},
                                                                    "label": {"template": "{number} · fotograma "
                                                                              "{frame} de {frames_total}"}},
                                                  "output": out("etiqueta_plantilla")})
    run("etiqueta_escena", "make_storyboard", {**lab, "panels": [{"action": "Primer plano"},
                                                                 {"number": "5.2A", "action": "Inserto añadido"}],
                                               "layout": {**lw, "template": "storyboard",
                                                          "numbering": {"format": "5.{n}"}},
                                               "output": out("etiqueta_escena")})
    # guías
    g = {"input": {"video": V}, "select": {"at_s": [8.6]}, "layout": {"panel_width": 480, "header": False,
                                                                      "footer": False, "label": {"show": False}}}
    run("guia_239", "make_storyboard", {**g, "layout": {**g["layout"], "guides": {"aspects": ["2.39:1"]}},
                                        "output": out("guia_239")})
    run("guia_916", "make_storyboard", {**g, "layout": {**g["layout"], "guides": {"aspects": ["9:16", "1:1"]}},
                                        "output": out("guia_916")})
    run("guia_segura", "make_storyboard", {**g, "layout": {**g["layout"], "guides": {"safe_zone": True,
                                                                                    "thirds": True}},
                                           "output": out("guia_segura")})
    # imágenes
    run("fotos_todas", "make_storyboard", {"input": {"folder": F}, "layout": {"panel_width": 300, "columns": 5},
                                           "output": out("fotos_todas")})
    run("fotos_mejores", "make_storyboard", {"input": {"folder": F}, "select": {"drop_blurriest_pct": 20,
                                                                                "min_difference": 4},
                                             "layout": {"panel_width": 300, "columns": 5,
                                                        "label": {"fields": ["number", "name", "sharpness"]}},
                                             "output": out("fotos_mejores")})
    run("fotos_best", "make_storyboard", {"input": {"folder": F}, "select": {"mode": "best", "count": 4},
                                          "layout": {"panel_width": 300, "columns": 4,
                                                     "label": {"fields": ["number", "name", "sharpness"]}},
                                          "output": out("fotos_best")})
    run("fotos_lista", "make_storyboard", {"input": {"images": [f"{F}/foto_01.jpg", f"{F}/foto_09_movil.jpg"]},
                                           "panels": [{"action": "Paisaje del día 1."},
                                                      {"action": "Foto vertical del móvil: se gira sola (EXIF)."}],
                                           "layout": {"template": "list", "panel_width": 300,
                                                      "label": {"fields": ["name", "size"]}},
                                           "style": {"theme": "light"}, "output": out("fotos_lista")})
    # PDF y lista de planos (y las mismas páginas en PNG para enseñarlas)
    pdf_args = {"input": {"video": V}, "select": {"count": 8},
                "panels": [{"shot": "PG", "action": "Amanece."}, {"shot": "PM", "camera": "Paneo"},
                           {"shot": "PP", "dialogue": "ANA: Llego tarde."}],
                "layout": {"template": "storyboard", "page": "A4", "title": "Escena 3 — Amanecer",
                           "numbering": {"format": "3.{n}"}},
                "style": {"theme": "light"}}
    run("pdf", "make_storyboard", {**pdf_args, "output": out("pdf", format="pdf", shot_list=["json", "csv"])})
    run("pdf_png", "make_storyboard", {**pdf_args, "output": out("pdf_png")})
    # usos (para el README)
    run("uso_tramo", "make_storyboard", {"input": {"video": V}, "select": {"start_s": 5, "end_s": 10,
                                                                          "every_s": 1},
                                         "layout": {"panel_width": 256, "columns": 5},
                                         "output": out("uso_tramo")})
    run("uso_movil", "make_storyboard", {"input": {"video": str(phone)}, "select": {"count": 4},
                                         "layout": {"template": "strip", "panel_width": 200},
                                         "output": out("uso_movil")})
    run("uso_rafaga", "extract_frames", {"input": {"folder": F}, "select": {"mode": "best", "count": 3},
                                         "output": {"dir": f"{O}/mejores", "names": "original"}})
    # extract_frames
    run("extraer_pct", "extract_frames", {"input": {"video": V}, "select": {"at_pct": [0, 50, 100]},
                                          "output": {"dir": f"{O}/extraer"}})
    run("extraer_snap", "extract_frames", {"input": {"video": V}, "select": {"at_frames": [90], "snap_s": 0.5},
                                           "output": {"dir": f"{O}/extraer_snap"}})
    run("extraer_fotos", "extract_frames", {"input": {"folder": F}, "select": {"mode": "sharpest", "count": 3},
                                            "output": {"dir": f"{O}/extraer_fotos"}})
    # planos, vídeo y guion de color
    shots = WORK / "planos.mp4"
    if not shots.exists():
        print("creando planos.mp4 …")
        make_shots_video(shots)
    P = str(shots)
    planos = run("planos", "make_storyboard", {"input": {"video": P}, "select": {"mode": "shots"},
                                               "layout": {"panel_width": 320, "columns": 3},
                                               "output": out("planos", shot_list=["json", "csv"], color_script=True)})
    run("mosaico", "make_storyboard", {"input": {"video": V}, "select": {"count": 6},
                                       "layout": {"panel_width": 320, "columns": 3},
                                       "output": out("mosaico", format="gif")})
    run("mosaico_readme", "make_storyboard", {"input": {"video": V}, "select": {"count": 6},
                                              "layout": {"panel_width": 320, "columns": 3},
                                              "output": out("mosaico_readme", format="webp", quality=60,
                                                            animation={"max_side": 640, "fps": 8})})
    run("animatica", "make_storyboard", {"input": {"video": V}, "select": {"at_s": [2.5, 9.5, 15.5]},
                                         "panels": [{"shot": "PG", "action": "Amanece sobre la ciudad.",
                                                     "duration_s": 2.5, "transition": "encadenado"},
                                                    {"shot": "PM", "dialogue": "ANA: Llego tarde.", "duration_s": 2,
                                                     "transition": "fundido a negro"},
                                                    {"shot": "PP", "action": "Mira el reloj.", "duration_s": 2.5}],
                                         "layout": {"template": "storyboard"},
                                         "output": out("animatica", format="mp4",
                                                       animation={"kind": "animatic", "max_side": 1280,
                                                                  "fade_s": 0.6})})
    run("animatica_planos", "make_storyboard", {"input": {"video": P}, "select": {"mode": "shots"},
                                                "layout": {"label": {"fields": ["number", "shot_in", "duration",
                                                                                "transition"]}},
                                                "output": out("animatica_planos", format="mp4",
                                                              animation={"kind": "animatic", "max_side": 1280})})
    run("presentacion", "make_storyboard", {"input": {"folder": F}, "select": {"mode": "best", "count": 4},
                                            "output": out("presentacion", format="webp",
                                                          animation={"panel_s": 2, "transition": "dissolve",
                                                                     "fade_s": 0.8})})
    print("figuras:")
    figures(minimo)
    video_figures(planos)


def figures(minimo: dict) -> None:
    S = WORK
    video_frames(S / "mosaico.gif", [1.0])[0].save(S / "mosaico_frame.png")
    anatomy_figure(S / "minimo.png", minimo, OUT / "fig_minimo.png")
    rows_figure("Plantillas de página (layout.template)",
                "Las mismas entradas con las cuatro plantillas. Las notas salen de panels[] (una por viñeta, en "
                "orden); sin notas, storyboard, list y strip dejan líneas para escribir a mano.", [
        ("grid: hoja de contactos (por defecto)", S / "plantilla_grid.png", ['"template": "grid"'],
         "Etiqueta encima de cada imagen, sin márgenes. Para ver un vídeo de un vistazo."),
        ("storyboard: número arriba, notas abajo", S / "plantilla_storyboard.png", ['"template": "storyboard"'],
         "Para planificar o contar un rodaje: plano, cámara, acción, diálogo, sonido, transición y duración."),
        ("list: imagen a la izquierda, notas a la derecha", S / "plantilla_list.png", ['"template": "list"'],
         "Una viñeta por fila, con sitio para notas largas."),
        ("strip: una sola fila", S / "plantilla_strip.png", ['"template": "strip"'],
         "Tira de viñetas con las notas debajo; con más de 8, una tira por página."),
    ], OUT / "fig_plantillas.png", img_w=900)
    rows_figure("Temas y colores (style)",
                "Tema oscuro (negro puro) o claro (blanco puro); cualquier color, fuente o tamaño se cambia "
                "con su campo, y lo que no se da sale del tema.", [
        ("Oscuro (por defecto)", S / "tema_oscuro.png", ['"style": {"theme": "dark"}'], "Fondo #000000."),
        ("Claro", S / "tema_claro.png", ['"style": {"theme": "light"}'], "Fondo #ffffff, para imprimir."),
        ("Colores y fuente propios", S / "tema_propio.png", ['"background": "#1e293b",', '"accent": "#f472b6",',
                                                               '"notes_background": "#0f172a",',
                                                               '"font": "Georgia" …'],
         "Cada color admite #RRGGBB, #RRGGBBAA (con transparencia) o un nombre CSS."),
    ], OUT / "fig_temas.png", img_w=900)
    rows_figure("Qué fotogramas se eligen (select)",
                "El vídeo de muestra se desenfoca en la primera mitad de cada segundo, como uno movido.", [
        ("El más nítido de cada tramo (defecto)", S / "sel_nitido.png", ['"count": 6'],
         "Divide el vídeo en 6 tramos y toma el menos movido de cada uno."),
        ("El del centro de cada tramo", S / "sel_centro.png", ['"count": 6, "mode": "even"'],
         "Reparto regular en el tiempo, aunque salga movido."),
        ("Posiciones exactas", S / "sel_exacto.png", ['"step_pct": 20'],
         "0, 20, 40, 60, 80 y 100 %: exactas, sin decodificar todo el vídeo. También at_pct, at_s, at_frames, "
         "step_s."),
        ("Exactas, el más nítido cerca", S / "sel_exacto_snap.png", ['"step_pct": 20, "snap_s": 0.5'],
         "En cada posición, el más nítido a ± 0,5 s."),
    ], OUT / "fig_seleccion.png")
    rows_figure("Filtros: sin movidos ni repetidos",
                "Lo que quitan los filtros sale en «dropped» del JSON y el umbral, en «selection.threshold».", [
        ("Sin filtros", S / "filtros_sin.png", ['"count": 10, "mode": "even"'],
         "La mitad sale movida y las dos últimas son el mismo plano fijo."),
        ("Con filtros", S / "filtros_con.png", ['"count": 10, "mode": "even",', '"drop_blurriest_pct": 50,',
                                                '"min_difference": 6'],
         "Ignora el 50 % más borroso y descarta la viñeta casi igual a la anterior."),
    ], OUT / "fig_filtros.png")
    rows_figure("Etiquetas y numeración (layout.label, layout.numbering)",
                "Los mismos dos fotogramas, el primero y el último del vídeo.", [
        ("Por defecto", S / "etiqueta_defecto.png", ["(sin parámetros)"],
         "Número · segundos · nº de fotograma; «primer / último fotograma» en los extremos."),
        ("Campos elegidos, abajo", S / "etiqueta_campos.png", ['"label": {"fields": ["number",', '  "timecode", '
                                                                                                  '"pct"],',
                                                                '  "position": "bottom"}'],
         "Los campos en el orden dado. Vídeo: number, frame, time, timecode, pct, sharpness, anchor. Imágenes: "
         "number, image, name, size, pct, sharpness."),
        ("Plantilla y numeración propia", S / "etiqueta_plantilla.png",
         ['"numbering": {"format":', '  "SH{n:03d}"},', '"label": {"template":', '  "{number} · fotograma',
          '  {frame} de {frames_total}"}'], "Texto libre con variables; números con formato de Python."),
        ("Escenas e insertos", S / "etiqueta_escena.png", ['"numbering": {"format": "5.{n}"}', 'panels[1].number:',
                                                          '  "5.2A"'],
         "En storyboard el número va grande encima; una viñeta puede llevar su propio número."),
    ], OUT / "fig_etiquetas.png", img_w=820)
    grid_figure("Guías sobre la imagen (layout.guides)",
                "Para ver qué quedaría al recortar a otro formato, la zona segura o la regla de los tercios.", [
        ("2.39:1 (cine)", S / "guia_239.png", ['"aspects": ["2.39:1"]'], "Oscurece lo que se perdería."),
        ("9:16 y 1:1", S / "guia_916.png", ['"aspects": ["9:16", "1:1"]'], "Varias a la vez; la primera sombrea."),
        ("Zona segura y tercios", S / "guia_segura.png", ['"safe_zone": true,', '"thirds": true'],
         "Zona segura del 90 % (safe_zone_pct)."),
    ], OUT / "fig_guias.png", cols=3)
    rows_figure("Entrada de imágenes (input.images o input.folder)",
                "Lo mismo que con vídeo, con el orden de la lista en vez del tiempo: nombre en vez de segundos.", [
        ("Una carpeta, todas", S / "fotos_todas.png", ['"input": {"folder": ".../fotos"}'],
         "Ordenadas por nombre (foto_2 antes que foto_10). La vertical del móvil sale derecha (EXIF)."),
        ("Las 4 más nítidas, sin repetir", S / "fotos_best.png", ['"mode": "best", "count": 4'],
         "Las más nítidas de toda la carpeta, en su orden; nunca la misma imagen dos veces."),
        ("Sin movidas ni repetidas", S / "fotos_mejores.png", ['"drop_blurriest_pct": 20,',
                                                               '"min_difference": 4'],
         "Todas menos las movidas (3 y 6) y la casi igual a la anterior (5b); salen en «dropped» del JSON."),
        ("Lista con notas", S / "fotos_lista.png", ['"input": {"images": [...]},', '"template": "list"'],
         "Una lista de rutas en el orden que quieras."),
    ], OUT / "fig_imagenes.png")
    rows_figure("Usos: lo que pides y lo que sale",
                "Peticiones típicas a un asistente, la llamada que hace y la imagen real que recibe (además del "
                "JSON con una entrada por viñeta).", [
        ("«Resume este vídeo»", S / "minimo.png", ['{"input": {"video": "…/ciudad.mp4"}}'],
         "Hoja de contactos con los 12 fotogramas más nítidos, uno por tramo."),
        ("«Del segundo 5 al 10, el mejor de cada segundo»", S / "uso_tramo.png",
         ['"select": {"start_s": 5,', '  "end_s": 10, "every_s": 1}'],
         "Solo esa parte del vídeo, el más nítido de cada segundo."),
        ("«Mira este vídeo del móvil»", S / "uso_movil.png", ['{"input": {"video": "…/movil.mp4"},',
                                                             ' "layout": {"template": "strip"}}'],
         "Grabado en vertical: sale derecho, con rotation_deg 90 en el JSON."),
        ("«Hazme un storyboard para imprimir»", S / "pdf_png_p1.png",
         ['"panels": [{"shot": "PG", …}],', '"layout": {"template":', '  "storyboard", "page": "A4"},',
          '"output": {"format": "pdf",', '  "shot_list": ["csv"]}'],
         "PDF A4 con notas por viñeta y la lista de planos en CSV."),
        ("«Quédate con las 4 mejores fotos»", S / "fotos_best.png", ['{"input": {"folder": "…/fotos"},',
                                                                     ' "select": {"mode": "best",',
                                                                     '   "count": 4}}'],
         "Las más nítidas y sin repetir. Con extract_frames se copian tal cual a otra carpeta."),
        ("«Divide el vídeo en planos»", S / "planos.png", ['"select": {"mode": "shots"}'],
         "Una viñeta por plano, con su entrada, su duración y si acaba en corte, fundido o encadenado."),
        ("«Hazme un GIF que se mueva»", S / "mosaico_frame.png", ['"output": {"format": "gif"}'],
         "La misma hoja con cada viñeta reproduciendo su tramo (aquí, un fotograma del GIF). También mp4, "
         "webm, webp y la animática."),
    ], OUT / "fig_usos.png")
    pages = sorted(S.glob("pdf_png_p*.png"))
    grid_figure("PDF de varias páginas y lista de planos",
                'storyboard en A4 apaisado ("page": "A4"), 8 viñetas en 2 páginas, con '
                '"format": "pdf" y "shot_list": ["json", "csv"]. Aquí, las mismas páginas en PNG.',
                [(f"Página {k}", p, [], "") for k, p in enumerate(pages, 1)], OUT / "fig_pdf.png", cols=2)
    ex = json.loads((OUT / "json" / "extraer_pct.json").read_text())["files"]
    sn = json.loads((OUT / "json" / "extraer_snap.json").read_text())["files"][0]
    items = [(Path(f["file"]).name, S / "extraer" / Path(f["file"]).name, [], f"Fotograma {f['frame']}, "
              f"{f['time_s']} s, {f['pct']} %.") for f in ex]
    items.append((Path(sn["file"]).name, S / "extraer_snap" / Path(sn["file"]).name, ['"at_frames": [90],',
                                                                                     '"snap_s": 0.5'],
                  f"Se pidió el 90 (movido): da el {sn['frame']}, el más nítido a ± 0,5 s."))
    grid_figure("extract_frames: fotogramas a tamaño completo",
                'Con "at_pct": [0, 50, 100] y, en el último, la búsqueda del más nítido. Cada uno se guarda '
                "como f<número de 5 cifras>.jpg (640×360 aquí).", items, OUT / "fig_extract_frames.png", cols=4)


def video_frames(path: Path, times: list[float]) -> list[Image.Image]:
    """Los fotogramas del vídeo escrito más cercanos a esos tiempos (decodificados del archivo)."""
    with av.open(str(path)) as c:
        frames = [(float(f.pts * f.time_base), f.to_image().convert("RGB")) for f in c.decode(video=0)]
    return [min(frames, key=lambda x: abs(x[0] - t))[1] for t in times]


def filmstrip_figure(title: str, subtitle: str, rows: list, out: Path) -> None:
    """Una fila por vídeo: título, sus fotogramas reales en fila con su tiempo, y la llamada."""
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    blocks = []
    for head, frames, code, text in rows:
        n = len(frames)
        fw = (FIG_W - 2 * PAD - 14 * (n - 1)) // n
        ims = [im.resize((fw, round(im.height * fw / im.width)), Image.LANCZOS) for im, _ in frames]
        th = max(im.height for im in ims)
        caps = [wrap(probe, cap, F_TEXT, fw) for _, cap in frames]
        cap_h = 30 * max(len(c) for c in caps)
        blocks.append((head, ims, caps, fw, th, cap_h, code, text,
                       side_text(probe, 0, 0, FIG_W - 2 * PAD, code, text, False)))
    head_h = heading(probe, title, subtitle)
    total = head_h + sum(44 + b[4] + 10 + b[5] + 16 + b[8] + GAP for b in blocks) + PAD - GAP
    fig = Image.new("RGB", (FIG_W, total), PAPER)
    d = ImageDraw.Draw(fig)
    y = heading(d, title, subtitle)
    for n, (head, ims, caps, fw, th, cap_h, code, text, st) in enumerate(blocks, 1):
        badge(d, PAD + 17, y + 15, n)
        d.text((PAD + 46, y + 15), head, font=F_HEAD, fill=INK, anchor="lm")
        y += 44
        for k, (im, cap) in enumerate(zip(ims, caps)):
            x = PAD + k * (fw + 14)
            fig.paste(im, (x, y))
            d.rectangle((x - 1, y - 1, x + im.width, y + im.height), outline=RULE)
            for j, ln in enumerate(cap):
                d.text((x, y + th + 10 + 30 * j), ln, font=F_TEXT, fill=MUTED)
        y += th + 10 + cap_h + 16
        side_text(d, PAD, y, FIG_W - 2 * PAD, code, text)
        y += st + GAP
        if n < len(blocks):
            d.line((PAD, y - GAP // 2, FIG_W - PAD, y - GAP // 2), fill=RULE, width=2)
    save_fig(fig, out)


TRANS_ES = {"cut": "corte", "dissolve": "encadenado", "fade_out": "fundido a negro", "fade_in": "desde negro",
            "start": "inicio", "end": "fin"}


def shots_figure(data: dict, out: Path) -> None:
    """La hoja real de planos y, debajo, la línea de tiempo que dibuja su JSON: cada plano con su
    color dominante, su entrada y su duración, y cada transición con su tipo y su largo."""
    sheet = fit(WORK / "planos.png", FIG_W - 2 * PAD)
    panels = data["panels"]
    end = panels[-1]["shot_out_s"]
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    title = "Planos: un fotograma por plano, con sus cortes, fundidos y encadenados"
    sub = ('make_storyboard con "select": {"mode": "shots"} sobre planos.mp4 (6 planos). Debajo, la línea de tiempo '
           "dibujada con el JSON de la respuesta: shot_in_s, shot_out_s, transition_in / transition_out y su largo.")
    p3 = panels[2]
    code = [f'"panels": [ …, {{"n": 3, "frame": {p3["frame"]}, "shot_in_s": {p3["shot_in_s"]}, '
            f'"shot_out_s": {p3["shot_out_s"]}, "duration_s": {p3["duration_s"]},',
            f'  "shot_frames": {p3["shot_frames"]}, "transition_in": "{p3["transition_in"]}", '
            f'"transition_in_s": {p3.get("transition_in_s")},',
            f'  "transition_out": "{p3["transition_out"]}", "transition_out_s": {p3.get("transition_out_s")}}}, …]']
    text = ("Cada viñeta es el fotograma más nítido de su plano, fuera de las transiciones. Los planos van seguidos: "
            "shot_out_s de uno es shot_in_s del siguiente. Un encadenado se reparte entre los dos planos; en un "
            "fundido a negro, el corte se pone en medio del negro.")
    head = heading(probe, title, sub)
    bar_y, bar_h = head + sheet.height + 70, 54
    st = side_text(probe, 0, 0, FIG_W - 2 * PAD, code, text, False)
    fig = Image.new("RGB", (FIG_W, bar_y + bar_h + 96 + st + PAD), PAPER)
    d = ImageDraw.Draw(fig)
    heading(d, title, sub)
    fig.paste(sheet, (PAD, head))
    d.rectangle((PAD - 1, head - 1, PAD + sheet.width, head + sheet.height), outline=RULE)
    x_of = lambda t: PAD + (FIG_W - 2 * PAD) * t / end                     # noqa: E731
    for p in panels:
        col = p["colors"][0]["hex"] if p.get("colors") else "#888888"
        x0, x1 = x_of(p["shot_in_s"]), x_of(p["shot_out_s"])
        d.rectangle((x0, bar_y, x1, bar_y + bar_h), fill=col)
        d.text(((x0 + x1) / 2, bar_y + bar_h / 2), str(p["n"]), font=F_HEAD, fill=PAPER, anchor="mm",
               stroke_width=3, stroke_fill=INK)
        d.text((x0 + 4, bar_y + bar_h + 8), f'{p["shot_in_s"]:g} s', font=F_CODE, fill=MUTED)
        for side, kind, length in (("in", p["transition_in"], p.get("transition_in_s")),
                                   ("out", p["transition_out"], p.get("transition_out_s"))):
            if length:                                     # gradual: a band over the transition
                a, b = (x0, x_of(p["shot_in_s"] + length / 2 if kind == "dissolve" else p["shot_in_s"] + length)) \
                    if side == "in" else (x_of(p["shot_out_s"] - (length / 2 if kind == "dissolve" else length)), x1)
                band = Image.new("RGBA", (max(1, round(b - a)), bar_h), (0, 0, 0, 0))
                bd = ImageDraw.Draw(band)
                for k in range(-bar_h, band.width, 8):
                    bd.line((k, bar_h, k + bar_h, 0), fill=(255, 255, 255, 170) if kind == "dissolve"
                            else (0, 0, 0, 200), width=3)
                fig.paste(band, (round(a), bar_y), band)
        if p["transition_out"] != "end":
            d.line((x1, bar_y - 10, x1, bar_y + bar_h + 4), fill=INK, width=3)
            label = TRANS_ES[p["transition_out"]]
            right = x1 + probe.textlength(label, font=F_CODE) / 2 > FIG_W - PAD
            last = p is panels[-1]                          # el final del vídeo: una línea más arriba
            d.text((x1, bar_y - (38 if last else 14)), label, font=F_CODE, fill=INK, anchor="rs" if right else "ms")
    d.text((FIG_W - PAD, bar_y + bar_h + 8), f"{end:g} s", font=F_CODE, fill=MUTED, anchor="ra")
    legend_y = bar_y + bar_h + 40
    d.text((PAD, legend_y), "Rayas blancas: encadenado. Rayas negras: fundido a negro. Línea: el cambio de plano.",
           font=F_TEXT, fill=MUTED)
    side_text(d, PAD, legend_y + 44, FIG_W - 2 * PAD, code, text)
    save_fig(fig, out)


def video_figures(planos: dict) -> None:
    S = WORK
    shots_figure(planos, OUT / "fig_planos.png")
    colores = json.loads((OUT / "json" / "planos.json").read_text())["panels"]
    rows_figure("Guion de color (output.color_script)",
                'Los colores dominantes de cada viñeta, en orden: <name>_colors.png y "colors" en el JSON de cada '
                "viñeta (hex y % de la imagen, de más a menos).", [
        ("planos.mp4, un plano por viñeta", S / "planos_colors.png",
         ['"select": {"mode": "shots"},', '"output": {"color_script": true}', "",
          f'"colors": [{{"hex": "{colores[1]["colors"][0]["hex"]}",',
          f'  "pct": {colores[1]["colors"][0]["pct"]}}}, …]'],
         "Cada columna: la imagen arriba (si cabe) y sus colores apilados según cuánto ocupan. color_count (5) "
         "dice cuántos por viñeta. Con muchas viñetas, las columnas se estrechan y se quitan las imágenes."),
    ], OUT / "fig_guion_color.png", img_w=760)
    m = json.loads((OUT / "json" / "mosaico.json").read_text())
    mv = m["videos"][0]
    gif = S / "mosaico.gif"
    times = [0.0, mv["duration_s"] / 2, mv["duration_s"] - 1 / mv["fps"]]
    mframes = video_frames(gif, times)
    shutil.copy(S / "mosaico_readme.webp", OUT / "mosaico.webp")         # animado, para el README
    a = json.loads((OUT / "json" / "animatica.json").read_text())
    av_ = a["videos"][0]
    pv = [p["video_s"] for p in a["panels"]]
    at = [sum(pv[0]) / 2, pv[0][1], pv[1][1] - 0.15, (pv[2][0] + pv[2][1]) / 2]
    acap = ["viñeta 1", "encadenado: mitad y mitad", "fundido a negro", "viñeta 3"]
    aframes = video_frames(S / "animatica.mp4", at)
    b = json.loads((OUT / "json" / "animatica_planos.json").read_text())
    bp = b["panels"]
    bt = [(bp[0]["video_s"][0] + bp[0]["video_s"][1]) / 2, bp[1]["video_s"][1],
          bp[2]["video_s"][1] - 0.1, bp[3]["video_s"][0] + 0.15, bp[4]["video_s"][1], bp[5]["video_s"][1] - 0.15]
    bcap = ["plano 1", "encadenado del 2 al 3", "fundido a negro", "desde negro", "encadenado del 5 al 6",
            "fundido final"]
    bframes = video_frames(S / "animatica_planos.mp4", bt)
    c = json.loads((OUT / "json" / "presentacion.json").read_text())
    cp = [p["video_s"] for p in c["panels"]]
    ct = [sum(cp[0]) / 2, cp[0][1], sum(cp[1]) / 2, cp[1][1], sum(cp[2]) / 2]
    names = [p["name"] for p in c["panels"]]
    ccap = [names[0], "encadenado", names[1], "encadenado", names[2]]
    cframes = video_frames(S / "presentacion.webp", ct)
    filmstrip_figure("Vídeo: mosaico animado y animática (mp4, webm, gif, webp)",
                     "Fotogramas sacados de los archivos que escribió el servidor. Cada llamada escribe además la hoja "
                     "fija (PNG), <name>_samples.png con fotogramas de muestra y el JSON: un asistente no puede "
                     "ver un vídeo.", [
        ("Mosaico (vídeo): la hoja con cada viñeta reproduciendo su tramo",
         [(im, f"{t:.2f} s") for im, t in zip(mframes, times)],
         ['"select": {"count": 6},', '"output": {"format": "gif"}'],
         f"El tramo de cada viñeta en bucle, todas a la vez, con una barra de progreso (la marca blanca es el "
         f"fotograma de la hoja). {mv['size'][0]}×{mv['size'][1]}, {mv['fps']} fps, {mv['duration_s']} s, "
         "en bucle. El JSON da clip_s y speed de cada viñeta."),
        ("Animática: las viñetas en secuencia, con su duración y su transición",
         [(im, f"{t:.1f} s · {cap}") for im, t, cap in zip(aframes, at, acap)],
         ['"panels": [{"duration_s": 2.5, "transition": "encadenado"}, {"duration_s": 2,',
          '  "transition": "fundido a negro"}, …], "layout": {"template": "storyboard"},',
          '"output": {"format": "mp4", "animation": {"kind": "animatic"}}'],
         f"Cada viñeta se dibuja como en la plantilla, con su número, su etiqueta y sus notas. "
         f"{av_['size'][0]}×{av_['size'][1]}, {av_['duration_s']} s. El JSON da video_s: cuándo sale cada viñeta."),
        ("Animática de planos: repite el montaje del vídeo",
         [(im, f"{t:.1f} s · {cap}") for im, t, cap in zip(bframes, bt, bcap)],
         ['"select": {"mode": "shots"},', '"output": {"format": "mp4", "animation": {"kind": "animatic"}}'],
         "Con mode \"shots\", cada plano dura lo mismo que en el vídeo y lleva el corte, el encadenado o el "
         "fundido que se detectó."),
        ("Con imágenes: una presentación temporizada",
         [(im, f"{t:.1f} s · {cap}") for im, t, cap in zip(cframes, ct, ccap)],
         ['"input": {"folder": ".../fotos"}, "select": {"mode": "best", "count": 4},',
          '"output": {"format": "webp", "animation": {"panel_s": 2, "transition": "dissolve"}}'],
         "Con imágenes, la salida animada es siempre una animática: cada foto dura panel_s segundos (o su "
         "duration_s en panels[]), y aquí se encadena con la siguiente."),
    ], OUT / "fig_video.png")


if __name__ == "__main__":
    main()
