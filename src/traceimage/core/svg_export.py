"""True-scale SVG export (Phase 4) with an optional Inkscape flavor.

The root <svg> is sized in millimetres with a matching viewBox, so the file
opens and prints at true physical size (see PLAN.md sec. 5):

  * width/height = content bounding box (across all objects' contours) plus the
    configured margin, multiplied by mm_per_pixel;
  * <g id="photo"> -- optional, the source photo base64-embedded and positioned
    to register with the vector layer; can be downscaled to keep size sane;
  * <g id="trace"> -- one compound <path> per object (M..Z outer + M..Z per
    hole) with fill-rule="evenodd" so holes render as holes.

Two flavors:
  * plain (default) -- a minimal SVG any browser opens;
  * inkscape -- adds inkscape/sodipodi namespaces, a <sodipodi:namedview> with
    document-units="mm", and marks the photo/trace groups as named, editable
    layers (photo locked) so the user can fine-tune nodes in Inkscape.

build_content() returns just the inner photo+trace fragment in master mm
coordinates (origin at the top-left of the margin box); it is reused by both
single-file export here and by tiled printing in tiling.py.

Pure arithmetic and string building (plus a lazy cv2 import only when embedding
the photo), so it ports cleanly to C/C++.
"""

import base64

from . import geometry as geo

_INKSCAPE_NS = "http://www.inkscape.org/namespaces/inkscape"
_SODIPODI_NS = "http://sodipodi.sourceforge.net/DTD/sodipodi-0.0.dtd"
_XLINK_NS = "http://www.w3.org/1999/xlink"


class ExportError(Exception):
    pass


def _num(value):
    """Compact number formatting: 3 decimals, trailing zeros stripped."""
    s = "%.3f" % (value,)
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def content_bbox_px(project):
    """Union bounding box (pixels) of every contour vertex across all objects.

    Raises ExportError if there is nothing to export.
    """
    points = []
    for obj in project.objects:
        for contour in obj.contours:
            points.extend(contour.points)
    if not points:
        raise ExportError("no traced contours to export")
    return geo.bbox_of_points(points)


def _path_d(contour, ox, oy, mpp):
    """One subpath string 'M x y L x y ... Z' in mm, or '' if degenerate."""
    pts = contour.points
    if len(pts) < 3:
        return ""
    parts = []
    for i, (x, y) in enumerate(pts):
        mx = (x - ox) * mpp
        my = (y - oy) * mpp
        cmd = "M" if i == 0 else "L"
        parts.append("%s %s %s" % (cmd, _num(mx), _num(my)))
    parts.append("Z")
    return " ".join(parts)


def _object_path(obj, ox, oy, mpp, filled):
    """A single compound <path> for one object (outer + holes), or '' if empty."""
    ordered = ([c for c in obj.contours if c.role != "hole"]
               + [c for c in obj.contours if c.role == "hole"])
    subpaths = [d for d in (_path_d(c, ox, oy, mpp) for c in ordered) if d]
    if not subpaths:
        return ""
    d = " ".join(subpaths)
    style = obj.style
    fill = style.fill if filled and style.fill != "none" else (
        "#cccccc" if filled else "none")
    return (
        '    <path d="%s" fill="%s" fill-rule="evenodd" '
        'stroke="%s" stroke-width="%s" stroke-linejoin="round" />'
        % (d, fill, style.stroke, _num(style.stroke_width_mm)))


def _photo_image_tag(image_bgr, ox, oy, mpp, pixel_w, pixel_h, downscale_max):
    """Build the <image> element with the embedded source photo."""
    import cv2  # lazy: only needed when embedding the photo

    img = image_bgr
    if downscale_max:
        longest = max(pixel_w, pixel_h)
        if longest > downscale_max:
            scale = float(downscale_max) / float(longest)
            new_w = max(1, int(round(pixel_w * scale)))
            new_h = max(1, int(round(pixel_h * scale)))
            img = cv2.resize(img, (new_w, new_h),
                             interpolation=cv2.INTER_AREA)

    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise ExportError("failed to encode embedded photo")
    b64 = base64.b64encode(buf.tobytes()).decode("ascii")

    x_mm = (0.0 - ox) * mpp
    y_mm = (0.0 - oy) * mpp
    w_mm = pixel_w * mpp
    h_mm = pixel_h * mpp
    href = "data:image/png;base64," + b64
    return (
        '    <image x="%s" y="%s" width="%s" height="%s" '
        'preserveAspectRatio="none" xlink:href="%s" href="%s" />\n'
        % (_num(x_mm), _num(y_mm), _num(w_mm), _num(h_mm), href, href))


def build_content(project, image_bgr=None, embed_photo=True,
                  downscale_max=None, filled=False, as_layers=False,
                  mm_per_pixel=None):
    """Build the inner photo+trace fragment and return (content, w_mm, h_mm).

    Coordinates use master mm space with origin (0, 0) at the top-left of the
    margin box. `as_layers` adds Inkscape layer attributes to the groups.

    `mm_per_pixel` overrides the project's calibration (used by tiling to apply
    a scale factor or an uncalibrated default). If omitted, the project must be
    calibrated. Raises ExportError otherwise or if there are no contours.
    """
    mpp = mm_per_pixel
    if mpp is None:
        calib = project.calibration
        if not calib.is_calibrated:
            raise ExportError(
                "calibrate the scale before exporting (mm/px unknown)")
        mpp = calib.mm_per_pixel

    bbox = content_bbox_px(project)
    margin_px = project.margin_mm / mpp
    ox = bbox.min_x - margin_px
    oy = bbox.min_y - margin_px
    w_mm = (bbox.width + 2.0 * margin_px) * mpp
    h_mm = (bbox.height + 2.0 * margin_px) * mpp

    if as_layers:
        photo_attrs = (' inkscape:groupmode="layer" inkscape:label="Photo"'
                       ' sodipodi:insensitive="true"')
        trace_attrs = (' inkscape:groupmode="layer" inkscape:label="Trace"')
    else:
        photo_attrs = ""
        trace_attrs = ""

    out = []
    if embed_photo:
        if image_bgr is None:
            raise ExportError("embed_photo requested but no image was provided")
        out.append('  <g id="photo"%s>\n' % photo_attrs)
        out.append(_photo_image_tag(image_bgr, ox, oy, mpp,
                                    project.pixel_width, project.pixel_height,
                                    downscale_max))
        out.append('  </g>\n')

    out.append('  <g id="trace"%s>\n' % trace_attrs)
    for obj in project.objects:
        path = _object_path(obj, ox, oy, mpp, filled)
        if path:
            out.append(path + "\n")
    out.append('  </g>\n')
    return "".join(out), w_mm, h_mm


def build_svg(project, image_bgr=None, embed_photo=True,
              downscale_max=None, filled=False, inkscape=False):
    """Build and return the full SVG document text for `project`.

    inkscape=True emits the Inkscape flavor (named layers + namedview).
    """
    content, w_mm, h_mm = build_content(
        project, image_bgr=image_bgr, embed_photo=embed_photo,
        downscale_max=downscale_max, filled=filled, as_layers=inkscape)

    out = []
    out.append('<?xml version="1.0" encoding="UTF-8"?>\n')
    if inkscape:
        out.append(
            '<svg xmlns="http://www.w3.org/2000/svg" '
            'xmlns:xlink="%s" xmlns:inkscape="%s" xmlns:sodipodi="%s" '
            'width="%smm" height="%smm" viewBox="0 0 %s %s">\n'
            % (_XLINK_NS, _INKSCAPE_NS, _SODIPODI_NS,
               _num(w_mm), _num(h_mm), _num(w_mm), _num(h_mm)))
        out.append(
            '  <sodipodi:namedview inkscape:document-units="mm" units="mm" '
            'showgrid="false" />\n')
    else:
        out.append(
            '<svg xmlns="http://www.w3.org/2000/svg" '
            'xmlns:xlink="%s" '
            'width="%smm" height="%smm" viewBox="0 0 %s %s">\n'
            % (_XLINK_NS, _num(w_mm), _num(h_mm), _num(w_mm), _num(h_mm)))

    out.append(content)
    out.append('</svg>\n')
    return "".join(out)
