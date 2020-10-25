"""
    weasyprint.images
    -----------------

    Fetch and decode images in various formats.

"""

import math
from io import BytesIO
from xml.etree import ElementTree

import cairosvg.parser
import cairosvg.surface
import pydyf
from PIL import Image

from .layout.percentages import percentage
from .logger import LOGGER
from .urls import URLFetchingError, fetch


class ImageLoadingError(ValueError):
    """An error occured when loading an image.

    The image data is probably corrupted or in an invalid format.

    """

    @classmethod
    def from_exception(cls, exception):
        name = type(exception).__name__
        value = str(exception)
        return cls(f'{name}: {value}' if value else name)


class RasterImage:
    def __init__(self, pillow_image, optimize_image):
        self.pillow_image = pillow_image
        self.optimize_image = optimize_image
        self._intrinsic_width = pillow_image.width
        self._intrinsic_height = pillow_image.height
        self.intrinsic_ratio = (
            self._intrinsic_width / self._intrinsic_height
            if self._intrinsic_height != 0 else float('inf'))

    def get_intrinsic_size(self, image_resolution, _font_size):
        # Raster images are affected by the 'image-resolution' property.
        return (self._intrinsic_width / image_resolution,
                self._intrinsic_height / image_resolution)

    def draw(self, context, concrete_width, concrete_height, image_rendering):
        has_size = (
            concrete_width > 0
            and concrete_height > 0
            and self._intrinsic_width > 0
            and self._intrinsic_height > 0
        )
        if not has_size:
            return

        image_name = context.add_image(
            self.pillow_image, image_rendering, self.optimize_image)
        # Use the real intrinsic size here,
        # not affected by 'image-resolution'.
        context.push_state()
        context.transform(
            concrete_width, 0, 0, -concrete_height, 0, concrete_height)
        context.draw_x_object(image_name)
        context.pop_state()


class ScaledSVGSurface(cairosvg.surface.SVGSurface):
    """
    Have the cairo Surface object have intrinsic dimension
    in pixels instead of points.
    """
    @property
    def device_units_per_user_units(self):
        scale = super().device_units_per_user_units
        return scale / 0.75


class FakeSurface:
    """Fake CairoSVG surface used to get SVG attributes."""
    context_height = 0
    context_width = 0
    font_size = 12
    dpi = 96


class SVGImage:
    def __init__(self, svg_data, base_url, url_fetcher):
        # Don’t pass data URIs to CairoSVG.
        # They are useless for relative URIs anyway.
        self._base_url = (
            base_url if not base_url.lower().startswith('data:') else None)
        self._svg_data = svg_data
        self._url_fetcher = url_fetcher

        try:
            self._tree = ElementTree.fromstring(self._svg_data)
        except Exception as e:
            raise ImageLoadingError.from_exception(e)

    def _cairosvg_url_fetcher(self, src, mimetype):
        data = self._url_fetcher(src)
        if 'string' in data:
            return data['string']
        return data['file_obj'].read()

    def get_intrinsic_size(self, _image_resolution, font_size):
        # Vector images may be affected by the font size.
        fake_surface = FakeSurface()
        fake_surface.font_size = font_size
        # Percentages don't provide an intrinsic size, we transform percentages
        # into 0 using a (0, 0) context size:
        # http://www.w3.org/TR/SVG/coords.html#IntrinsicSizing
        self._width = cairosvg.surface.size(
            fake_surface, self._tree.get('width'))
        self._height = cairosvg.surface.size(
            fake_surface, self._tree.get('height'))
        _, _, viewbox = cairosvg.surface.node_format(fake_surface, self._tree)
        self._intrinsic_width = self._width or None
        self._intrinsic_height = self._height or None
        self.intrinsic_ratio = None
        if viewbox:
            if self._width and self._height:
                self.intrinsic_ratio = self._width / self._height
            else:
                if viewbox[2] and viewbox[3]:
                    self.intrinsic_ratio = viewbox[2] / viewbox[3]
                    if self._width:
                        self._intrinsic_height = (
                            self._width / self.intrinsic_ratio)
                    elif self._height:
                        self._intrinsic_width = (
                            self._height * self.intrinsic_ratio)
        elif self._width and self._height:
            self.intrinsic_ratio = self._width / self._height
        return self._intrinsic_width, self._intrinsic_height

    def draw(self, context, concrete_width, concrete_height, _image_rendering):
        try:
            svg = ScaledSVGSurface(
                cairosvg.parser.Tree(
                    bytestring=self._svg_data, url=self._base_url,
                    url_fetcher=self._cairosvg_url_fetcher),
                output=None, dpi=96, output_width=concrete_width,
                output_height=concrete_height)
            if svg.width and svg.height:
                context.scale(
                    concrete_width / svg.width, concrete_height / svg.height)
                context.set_source_surface(svg.cairo)
                context.paint()
        except Exception as exception:
            LOGGER.error(
                'Failed to draw an SVG image at %r: %s',
                self._base_url, exception)


def get_image_from_uri(cache, url_fetcher, optimize_images, url,
                       forced_mime_type=None):
    """Get a cairo Pattern from an image URI."""
    missing = object()
    image = cache.get(url, missing)
    if image is not missing:
        return image

    try:
        with fetch(url_fetcher, url) as result:
            if 'string' in result:
                string = result['string']
            else:
                string = result['file_obj'].read()
            mime_type = forced_mime_type or result['mime_type']
            if mime_type == 'image/svg+xml':
                # No fallback for XML-based mimetypes as defined by MIME
                # Sniffing Standard, see https://mimesniff.spec.whatwg.org/
                image = SVGImage(string, url, url_fetcher)
            else:
                # Try to rely on given mimetype
                try:
                    pillow_image = Image.open(BytesIO(string))
                except Exception as exception:
                    raise ImageLoadingError.from_exception(exception)
                else:
                    image = RasterImage(pillow_image, optimize_images)

    except (URLFetchingError, ImageLoadingError) as exception:
        LOGGER.error('Failed to load image at %r: %s', url, exception)
        image = None
    cache[url] = image
    return image


def process_color_stops(vector_length, positions):
    """Give color stops positions on the gradient vector.

    ``vector_length`` is the distance between the starting point and ending
    point of the vector gradient.

    ``positions`` is a list of ``None``, or ``Dimension`` in px or %. 0 is the
    starting point, 1 the ending point.

    See http://dev.w3.org/csswg/css-images-3/#color-stop-syntax.

    Return processed color stops, as a list of floats in px.

    """
    # Resolve percentages
    positions = [percentage(position, vector_length) for position in positions]

    # First and last default to 100%
    if positions[0] is None:
        positions[0] = 0
    if positions[-1] is None:
        positions[-1] = vector_length

    # Make sure positions are increasing
    previous_pos = positions[0]
    for i, position in enumerate(positions):
        if position is not None:
            if position < previous_pos:
                positions[i] = previous_pos
            else:
                previous_pos = position

    # Assign missing values
    previous_i = -1
    for i, position in enumerate(positions):
        if position is not None:
            base = positions[previous_i]
            increment = (position - base) / (i - previous_i)
            for j in range(previous_i + 1, i):
                positions[j] = base + j * increment
            previous_i = i

    return positions


def normalize_stop_positions(positions):
    """Normalize stop positions between 0 and 1.

    Return ``(first, last, positions)``.

    first: original position of the first position.
    last: original position of the last position.
    positions: list of positions between 0 and 1.

    """
    first, last = positions[0], positions[-1]
    total_length = last - first
    if total_length == 0:
        positions = [0] * len(positions)
    else:
        positions = [(pos - first) / total_length for pos in positions]
    return first, last, positions


def gradient_average_color(colors, positions):
    """
    http://dev.w3.org/csswg/css-images-3/#gradient-average-color
    """
    nb_stops = len(positions)
    assert nb_stops > 1
    assert nb_stops == len(colors)
    total_length = positions[-1] - positions[0]
    if total_length == 0:
        positions = list(range(nb_stops))
        total_length = nb_stops - 1
    premul_r = [r * a for r, g, b, a in colors]
    premul_g = [g * a for r, g, b, a in colors]
    premul_b = [b * a for r, g, b, a in colors]
    alpha = [a for r, g, b, a in colors]
    result_r = result_g = result_b = result_a = 0
    total_weight = 2 * total_length
    for i, position in enumerate(positions[1:], 1):
        weight = (position - positions[i - 1]) / total_weight
        for j in (i - 1, i):
            result_r += premul_r[j] * weight
            result_g += premul_g[j] * weight
            result_b += premul_b[j] * weight
            result_a += alpha[j] * weight
    # Un-premultiply:
    return (result_r / result_a, result_g / result_a,
            result_b / result_a, result_a) if result_a != 0 else (0, 0, 0, 0)


class Gradient:
    def __init__(self, color_stops, repeating):
        assert color_stops
        #: List of (r, g, b, a), list of Dimension
        self.colors = tuple(color for color, position in color_stops)
        self.stop_positions = tuple(position for _, position in color_stops)
        #: bool
        self.repeating = repeating

    def get_intrinsic_size(self, _image_resolution, _font_size):
        # Gradients are not affected by image resolution, parent or font size.
        return None, None

    intrinsic_ratio = None

    def draw(self, context, concrete_width, concrete_height, _image_rendering):
        # TODO: handle alpha and color spaces
        scale_y, type_, points, positions, colors = self.layout(
            concrete_width, concrete_height)

        if type_ == 'solid':
            context.rectangle(0, 0, concrete_width, concrete_height)
            context.set_color_rgb(*colors[0][:3])
            context.fill()
            return

        shading = context.add_shading()
        shading['ShadingType'] = 2 if type_ == 'linear' else 3
        shading['ColorSpace'] = '/DeviceRGB'
        shading['Domain'] = pydyf.Array([positions[0], positions[-1]])
        shading['Coords'] = pydyf.Array(points)
        shading['Function'] = pydyf.Dictionary({
            'FunctionType': 3,
            'Domain': pydyf.Array([positions[0], positions[-1]]),
            'Encode': pydyf.Array((len(colors) - 1) * [0, 1]),
            'Bounds': pydyf.Array(positions[1:-1]),
            'Functions': pydyf.Array([
                pydyf.Dictionary({
                    'FunctionType': 2,
                    'Domain': pydyf.Array([0, 1]),
                    'C0': pydyf.Array(colors[i][:3]),
                    'C1': pydyf.Array(colors[i + 1][:3]),
                    'N': 1,
                }) for i in range(len(colors) - 1)
            ]),
        })
        if not self.repeating:
            shading['Extend'] = pydyf.Array([b'true', b'true'])
        context.transform(1, 0, 0, scale_y, 0, 0)
        context.shading(shading.id)

    def layout(self, width, height):
        """Get layout information about the gradient.

        width, height: Gradient box. Top-left is at coordinates (0, 0).

        Returns (scale_y, type_, points, positions, colors).

        scale_y: vertical scale of the gradient. float, used for ellipses
                 radial gradients. 1 otherwise.
        type_: gradient type.
        points: coordinates of useful points, depending on type_:
            'solid': None.
            'linear': (x0, y0, x1, y1)
                      coordinates of the starting and ending points.
            'radial': (cx0, cy0, radius0, cx1, cy1, radius1)
                      coordinates of the starting end ending circles
        positions: positions of the color stops. list of floats in between 0
                   and 1 (0 at the starting point, 1 at the ending point).
        colors: list of (r, g, b, a).

        """
        raise NotImplementedError


class LinearGradient(Gradient):
    def __init__(self, color_stops, direction, repeating):
        Gradient.__init__(self, color_stops, repeating)
        # ('corner', keyword) or ('angle', radians)
        self.direction_type, self.direction = direction

    def layout(self, width, height):
        # Only one color, render the gradient as a solid color
        if len(self.colors) == 1:
            return 1, 'solid', None, [], [self.colors[0]]

        # Define the (dx, dy) unit vector giving the direction of the gradient.
        # Positive dx: right, positive dy: down.
        if self.direction_type == 'corner':
            y, x = self.direction.split('_')
            factor_x = -1 if x == 'left' else 1
            factor_y = -1 if y == 'top' else 1
            diagonal = math.hypot(width, height)
            # Note the direction swap: dx based on height, dy based on width
            # The gradient line is perpendicular to a diagonal.
            dx = factor_x * height / diagonal
            dy = factor_y * width / diagonal
        else:
            assert self.direction_type == 'angle'
            angle = self.direction  # 0 upwards, then clockwise
            dx = math.sin(angle)
            dy = -math.cos(angle)

        # Normalize colors positions
        colors = list(self.colors)
        vector_length = abs(width * dx) + abs(height * dy)
        positions = process_color_stops(vector_length, self.stop_positions)
        if not self.repeating:
            # Add explicit colors at boundaries if needed, because PDF doesn’t
            # extend color stops that are not displayed
            if positions[0] == positions[1]:
                positions.insert(0, positions[0] - 1)
                colors.insert(0, colors[0])
            if positions[-2] == positions[-1]:
                positions.append(positions[-1] + 1)
                colors.append(colors[-1])
        first, last, positions = normalize_stop_positions(positions)

        # Render as a solid color if the first and last positions are the same
        # See https://drafts.csswg.org/css-images-3/#repeating-gradients
        if first == last and self.repeating:
            color = gradient_average_color(colors, positions)
            return 1, 'solid', None, [], [color]

        # Define the coordinates of the starting and ending points
        start_x = (width - dx * vector_length) / 2
        start_y = (height - dy * vector_length) / 2
        points = (
            start_x + dx * first, start_y + dy * first,
            start_x + dx * last, start_y + dy * last)

        return 1, 'linear', points, positions, colors


class RadialGradient(Gradient):
    def __init__(self, color_stops, shape, size, center, repeating):
        Gradient.__init__(self, color_stops, repeating)
        # Center of the ending shape. (origin_x, pos_x, origin_y, pos_y)
        self.center = center
        # Type of ending shape: 'circle' or 'ellipse'
        self.shape = shape
        # size_type: 'keyword'
        #   size: 'closest-corner', 'farthest-corner',
        #         'closest-side', or 'farthest-side'
        # size_type: 'explicit'
        #   size: (radius_x, radius_y)
        self.size_type, self.size = size

    def layout(self, width, height):
        # Only one color, render the gradient as a solid color
        if len(self.colors) == 1:
            return 1, 'solid', None, [], [self.colors[0]]

        # Define the center of the gradient
        origin_x, center_x, origin_y, center_y = self.center
        center_x = percentage(center_x, width)
        center_y = percentage(center_y, height)
        if origin_x == 'right':
            center_x = width - center_x
        if origin_y == 'bottom':
            center_y = height - center_y

        # Resolve sizes and vertical scale
        size_x, size_y = self._handle_degenerate(
            *self._resolve_size(width, height, center_x, center_y))
        scale_y = size_y / size_x

        # Normalize colors positions
        colors = list(self.colors)
        positions = process_color_stops(size_x, self.stop_positions)
        if not self.repeating:
            # Add explicit colors at boundaries if needed, because PDF doesn’t
            # extend color stops that are not displayed
            if positions[0] > 0 and positions[0] == positions[1]:
                positions.insert(0, 0)
                colors.insert(0, colors[0])
            if positions[-2] == positions[-1]:
                positions.append(positions[-1] + 1)
                colors.append(colors[-1])
        if positions[0] < 0:
            # PDF doesn’t like negative radiuses, shift into the positive realm
            if self.repeating:
                # Add vector lengths to first position until positive
                vector_length = positions[-1] - positions[0]
                offset = vector_length * (1 + (-positions[0] // vector_length))
                positions = [position + offset for position in positions]
            else:
                # Only keep colors with position >= 0, interpolate if needed
                if positions[-1] <= 0:
                    # All stops are negative, fill with the last color
                    return 1, 'solid', None, [], [self.colors[-1]]
                for i, position in enumerate(positions):
                    if position == 0:
                        # Keep colors and positions from this rank
                        colors, positions = colors[i:], positions[i:]
                        break
                    if position > 0:
                        # Interpolate with previous rank to get color at 0
                        color = colors[i]
                        previous_color = colors[i - 1]
                        previous_position = positions[i - 1]
                        assert previous_position < 0
                        intermediate_color = gradient_average_color(
                            [previous_color, previous_color, color, color],
                            [previous_position, 0, 0, position])
                        colors = [intermediate_color] + colors[i:]
                        positions = [0] + positions[i:]
                        break
        first, last, positions = normalize_stop_positions(positions)

        # Render as a solid color if the first and last positions are the same
        # See https://drafts.csswg.org/css-images-3/#repeating-gradients
        if first == last and self.repeating:
            color = gradient_average_color(colors, positions)
            return 1, 'solid', None, [], [color]

        # Define the coordinates of the gradient circles
        points = (
            center_x, center_y / scale_y, first,
            center_x, center_y / scale_y, last)

        if self.repeating:
            points, positions, colors = self._repeat(
                width, height, scale_y, points, positions, colors)

        return scale_y, 'radial', points, positions, colors

    def _repeat(self, width, height, scale_y, points, positions, colors):
        # Keep original lists and values, they’re useful
        original_colors = colors.copy()
        original_positions = positions.copy()
        gradient_length = points[5] - points[2]

        # Get the maximum distance between the center and the corners, to find
        # how many times we have to repeat the colors outside
        max_distance = max(
            math.hypot(width - points[0], height / scale_y - points[1]),
            math.hypot(width - points[0], -points[1] * scale_y),
            math.hypot(-points[0], height / scale_y - points[1]),
            math.hypot(-points[0], -points[1] * scale_y))
        repeat_after = math.ceil((max_distance - points[5]) / gradient_length)
        if repeat_after > 0:
            # Repeat colors and extrapolate positions
            repeat = 1 + repeat_after
            colors *= repeat
            positions = [
                i + position for i in range(repeat) for position in positions]
            points = points[:5] + (points[5] + gradient_length * repeat_after,)

        if points[2] == 0:
            # Inner circle has 0 radius, no need to repeat inside, return
            return points, positions, colors

        # Find how many times we have to repeat the colors inside
        repeat_before = points[2] / gradient_length

        # Set the inner circle size to 0
        points = points[:2] + (0,) + points[3:]

        # Find how many times the whole gradient can be repeated
        full_repeat = int(repeat_before)
        if full_repeat:
            # Repeat colors and extrapolate positions
            colors += original_colors * full_repeat
            positions = [
                i - full_repeat + position for i in range(full_repeat)
                for position in original_positions] + positions

        # Find the ratio of gradient that must be added to reach the center
        partial_repeat = repeat_before - full_repeat
        if partial_repeat == 0:
            # No partial repeat, return
            return points, positions, colors

        # Iterate through positions in reverse order, from the outer
        # circle to the original inner circle, to find positions from
        # the inner circle (including full repeats) to the center
        assert (original_positions[0], original_positions[-1]) == (0, 1)
        assert 0 < partial_repeat < 1
        reverse = original_positions[::-1]
        ratio = 1 - partial_repeat
        for i, position in enumerate(reverse, start=1):
            if position == ratio:
                # The center is a color of the gradient, truncate original
                # colors and positions and prepend them
                colors = original_colors[-i:] + colors
                new_positions = [
                    position - full_repeat - 1
                    for position in original_positions[-i:]]
                positions = new_positions + positions
                return points, positions, colors
            if position < ratio:
                # The center is between two colors of the gradient,
                # define the center color as the average of these two
                # gradient colors
                color = original_colors[-i]
                next_color = original_colors[-(i - 1)]
                next_position = original_positions[-(i - 1)]
                average_colors = [color, color, next_color, next_color]
                average_positions = [position, ratio, ratio, next_position]
                zero_color = gradient_average_color(
                    average_colors, average_positions)
                colors = [zero_color] + original_colors[-(i - 1):] + colors
                new_positions = [
                    position - 1 - full_repeat for position
                    in original_positions[-(i - 1):]]
                positions = (
                    [ratio - 1 - full_repeat] + new_positions + positions)
                return points, positions, colors

    def _resolve_size(self, width, height, center_x, center_y):
        """Resolve circle size of the radial gradient."""
        if self.size_type == 'explicit':
            size_x, size_y = self.size
            size_x = percentage(size_x, width)
            size_y = percentage(size_y, height)
            return size_x, size_y
        left = abs(center_x)
        right = abs(width - center_x)
        top = abs(center_y)
        bottom = abs(height - center_y)
        pick = min if self.size.startswith('closest') else max
        if self.size.endswith('side'):
            if self.shape == 'circle':
                size_xy = pick(left, right, top, bottom)
                return size_xy, size_xy
            # else: ellipse
            return pick(left, right), pick(top, bottom)
        # else: corner
        if self.shape == 'circle':
            size_xy = pick(math.hypot(left, top), math.hypot(left, bottom),
                           math.hypot(right, top), math.hypot(right, bottom))
            return size_xy, size_xy
        # else: ellipse
        corner_x, corner_y = pick(
            (left, top), (left, bottom), (right, top), (right, bottom),
            key=lambda a: math.hypot(*a))
        return corner_x * math.sqrt(2), corner_y * math.sqrt(2)

    def _handle_degenerate(self, size_x, size_y):
        """Handle degenerate radial gradients.

        See https://drafts.csswg.org/css-images-3/#degenerate-radials

        """
        if size_x == size_y == 0:
            size_x = size_y = 1e-7
        elif size_x == 0:
            size_x = 1e-7
            size_y = 1e7
        elif size_y == 0:
            size_x = 1e7
            size_y = 1e-7
        return size_x, size_y
