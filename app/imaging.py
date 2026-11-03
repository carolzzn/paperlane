import io
from functools import lru_cache
from PIL import Image, ImageCms, ImageOps, ImageDraw
from pillow_heif import register_heif_opener

register_heif_opener(thumbnails=False)
Image.MAX_IMAGE_PIXELS = 90_000_000

PAPERS = {
    '6inch': {'label': '6 寸相纸 · 4 × 6 英寸', 'pixels': (1200, 1800), 'hundredths_mm': (10160, 15240), 'media': 'na_index-4x6_4x6in'},
    '3inch': {'label': '3 寸方形相纸 · 3 × 3 英寸', 'pixels': (900, 900), 'hundredths_mm': (7620, 7620), 'media': 'oe_photo_3x3in'},
}
LAYOUT_CAPACITY = {'single': 1, 'duo': 2, 'grid4': 4, 'grid9': 9, 'square3': 2}


@lru_cache(maxsize=3)
def icon_png(size):
    icon = Image.new('RGB', (512, 512), '#1b514d')
    card = Image.new('RGBA', (330, 390))
    draw = ImageDraw.Draw(card)
    draw.rounded_rectangle((15, 15, 315, 375), radius=17, fill='#f8f5eb')
    draw.rectangle((43, 43, 287, 281), fill='#c9d8ba')
    draw.ellipse((205, 63, 265, 123), fill='#e5ad66')
    draw.polygon([(43, 250), (120, 140), (190, 225), (235, 180), (287, 250), (287, 281), (43, 281)], fill='#497b65')
    card = card.rotate(8, expand=True, resample=Image.Resampling.BICUBIC)
    icon.paste(card, ((512 - card.width) // 2, (512 - card.height) // 2), card)
    icon = icon.resize((size, size), Image.Resampling.LANCZOS)
    result = io.BytesIO()
    icon.save(result, 'PNG')
    return result.getvalue()


def open_photo(path):
    with Image.open(path) as original:
        profile = original.info.get('icc_profile')
        original.load()
        photo = ImageOps.exif_transpose(original).convert('RGB')
        if profile:
            try:
                photo = ImageCms.profileToProfile(photo, ImageCms.ImageCmsProfile(io.BytesIO(profile)), ImageCms.createProfile('sRGB'), outputMode='RGB')
            except (ValueError, OSError, ImageCms.PyCMSError):
                # Some phone profiles are incomplete. Keep a usable RGB image.
                pass
        return photo


def render_photo(path, fit='contain', rotation=0):
    photo = open_photo(path)
    if rotation:
        photo = photo.rotate(-rotation, expand=True)
    landscape = photo.width > photo.height
    size = (1800, 1200) if landscape else (1200, 1800)
    if fit == 'cover':
        output = ImageOps.fit(photo, size, method=Image.Resampling.LANCZOS)
    else:
        fitted = ImageOps.contain(photo, size, method=Image.Resampling.LANCZOS)
        output = Image.new('RGB', size, 'white')
        output.paste(fitted, ((size[0] - fitted.width) // 2, (size[1] - fitted.height) // 2))
    buf = io.BytesIO()
    output.save(buf, 'JPEG', quality=95, subsampling=0, dpi=(300, 300))
    return buf.getvalue(), landscape, photo.size


def thumbnail(path):
    photo = open_photo(path)
    photo.thumbnail((480, 480), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    photo.save(buf, 'JPEG', quality=78)
    return buf.getvalue()


def render_sheet(items, paper='6inch', layout='single', fit='contain', border_mm=0, gap_mm=2, background='#ffffff'):
    """Render the exact, metadata-free JPEG which will be previewed and sent.

    items: path/rotation/x/y; never change the queue originals.
    """
    spec = PAPERS[paper]
    if layout == 'square3' and paper != '6inch':
        raise ValueError('3 寸方形双拼需要使用 6 寸相纸')
    canvas_size = spec['pixels']
    first = None
    if layout == 'single':
        first = open_photo(items[0]['path'])
        if items[0].get('rotation'):
            first = first.rotate(-items[0]['rotation'], expand=True)
    landscape = first is not None and first.width > first.height and paper == '6inch'
    if landscape:
        canvas_size = tuple(reversed(canvas_size))
    canvas = Image.new('RGB', canvas_size, background)
    border = round(border_mm / 25.4 * 300)
    gap = round(gap_mm / 25.4 * 300)
    if layout == 'square3':
        # Two exact 3 × 3 inch squares on one 4 × 6 sheet, without a gutter.
        slots = [(150, 0, 1050, 900), (150, 900, 1050, 1800)]
    else:
        cols, rows = {'single': (1, 1), 'duo': (1, 2), 'grid4': (2, 2), 'grid9': (3, 3)}[layout]
        width = (canvas_size[0] - 2 * border - (cols - 1) * gap) // cols
        height = (canvas_size[1] - 2 * border - (rows - 1) * gap) // rows
        slots = [(border + col * (width + gap), border + row * (height + gap), border + col * (width + gap) + width, border + row * (height + gap) + height) for row in range(rows) for col in range(cols)]
    source_sizes = []
    for item, box in zip(items, slots):
        image = first if first is not None else open_photo(item['path'])
        if first is None and item.get('rotation'):
            image = image.rotate(-item['rotation'], expand=True)
        source_sizes.append(image.size)
        size = (box[2] - box[0], box[3] - box[1])
        if fit == 'cover':
            tile = ImageOps.fit(image, size, method=Image.Resampling.LANCZOS, centering=(item.get('x', .5), item.get('y', .5)))
            xy = box[:2]
        else:
            tile = ImageOps.contain(image, size, method=Image.Resampling.LANCZOS)
            xy = (box[0] + (size[0] - tile.width) // 2, box[1] + (size[1] - tile.height) // 2)
        canvas.paste(tile, xy)
        del tile, image
    if layout == 'square3':
        draw = ImageDraw.Draw(canvas)
        # Cut marks are outside the square image area, not across the photos.
        for x in (150, 1050):
            for y in (0, 900, 1799):
                draw.line((x - 18, y, x - 4, y), fill='#cccccc', width=1)
                draw.line((x + 4, y, x + 18, y), fill='#cccccc', width=1)
    buf = io.BytesIO()
    canvas.save(buf, 'JPEG', quality=95, subsampling=0, dpi=(300, 300))
    return buf.getvalue(), landscape, source_sizes
