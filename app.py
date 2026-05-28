from flask import Flask, render_template, request, jsonify
import requests
import re

app = Flask(__name__)

COMPASS_FULL = [
    'North', 'North-Northeast', 'Northeast', 'East-Northeast',
    'East', 'East-Southeast', 'Southeast', 'South-Southeast',
    'South', 'South-Southwest', 'Southwest', 'West-Southwest',
    'West', 'West-Northwest', 'Northwest', 'North-Northwest',
]

WX_CODES = {
    'DZ': 'drizzle', 'RA': 'rain', 'SN': 'snow', 'SG': 'snow grains',
    'IC': 'ice crystals', 'PL': 'ice pellets', 'GR': 'hail',
    'GS': 'small hail', 'UP': 'unknown precipitation',
    'BR': 'mist', 'FG': 'fog', 'FU': 'smoke', 'VA': 'volcanic ash',
    'DU': 'dust', 'SA': 'sand', 'HZ': 'haze', 'PY': 'spray',
    'PO': 'dust whirls', 'SQ': 'squall', 'FC': 'tornado/waterspout',
    'SS': 'sandstorm', 'DS': 'dust storm', 'TS': 'thunderstorm',
}

DESC_CODES = {
    'MI': 'shallow', 'BC': 'patchy', 'PR': 'partial',
    'DR': 'low drifting', 'BL': 'blowing', 'SH': 'showers of',
    'TS': 'thunderstorm with', 'FZ': 'freezing',
}

SKY_LABELS = {
    'SKC': 'Clear skies', 'CLR': 'Clear skies', 'NSC': 'No significant clouds',
    'NCD': 'No clouds detected', 'CAVOK': 'Clear skies, visibility OK',
    'FEW': 'Few clouds', 'SCT': 'Scattered clouds',
    'BKN': 'Broken cloud layer', 'OVC': 'Overcast', 'VV': 'Sky obscured',
}

SKY_RANK = {
    'SKC': 0, 'CLR': 0, 'NSC': 0, 'NCD': 0, 'CAVOK': 0,
    'FEW': 1, 'SCT': 2, 'BKN': 3, 'OVC': 4, 'VV': 5,
}

SKY_HEADLINE = {
    'clear': 'Clear', 'CLR': 'Clear', 'SKC': 'Clear', 'NSC': 'Clear', 'NCD': 'Clear', 'CAVOK': 'Clear',
    'FEW': 'Mostly clear', 'SCT': 'Partly cloudy',
    'BKN': 'Mostly cloudy', 'OVC': 'Overcast', 'VV': 'Sky obscured',
}


def deg_to_compass(deg):
    return COMPASS_FULL[round(int(deg) / 22.5) % 16]


def parse_wx_token(token):
    m = re.match(
        r'^(-|\+|VC)?(MI|BC|PR|DR|BL|SH|TS|FZ)?((?:DZ|RA|SN|SG|IC|PL|GR|GS|UP|BR|FG|FU|VA|DU|SA|HZ|PY|PO|SQ|FC|SS|DS|TS)+)$',
        token,
    )
    if not m:
        return None

    prefix = {'−': 'light ', '-': 'light ', '+': 'heavy ', 'VC': ''}.get(m.group(1), '')
    vc_suffix = ' nearby' if m.group(1) == 'VC' else ''
    desc = (DESC_CODES.get(m.group(2), '') + ' ') if m.group(2) else ''

    phenomena = m.group(3)
    phenom_words = []
    i = 0
    while i < len(phenomena):
        code = phenomena[i:i+2]
        if code in WX_CODES:
            phenom_words.append(WX_CODES[code])
        i += 2

    return (prefix + desc + '/'.join(phenom_words) + vc_suffix).strip()


def decode_metar(raw):
    raw = re.sub(r'^(METAR|SPECI)\s+', '', raw.strip())
    tokens = raw.split()
    result = {
        'raw': raw,
        'station': None,
        'time_utc': None,
        'wind_desc': None,
        'wind_speed_mph': None,
        'visibility_desc': None,
        'weather': [],
        'sky_layers': [],
        'sky_overall': 'clear',
        'temp_c': None, 'temp_f': None,
        'dew_c': None, 'dew_f': None,
        'pressure_inhg': None,
        'headline': None,
    }

    i = 0

    # Station ID
    if i < len(tokens):
        result['station'] = tokens[i]
        i += 1

    # Timestamp DDHHMMZ
    if i < len(tokens) and re.match(r'^\d{6}Z$', tokens[i]):
        t = tokens[i]
        result['time_utc'] = f"{t[2:4]}:{t[4:6]} UTC"
        i += 1

    # AUTO / COR / RTD
    if i < len(tokens) and tokens[i] in ('AUTO', 'COR', 'RTD'):
        i += 1

    # Wind
    if i < len(tokens):
        wm = re.match(r'^(VRB|\d{3})(\d{2,3})(G(\d{2,3}))?(KT|MPS)$', tokens[i])
        if wm:
            direction, speed_raw, gust_raw, unit = wm.group(1), int(wm.group(2)), wm.group(4), wm.group(5)
            kt_to_mph = 1.15078 if unit == 'KT' else 2.23694
            speed_mph = round(speed_raw * kt_to_mph)
            gust_mph = round(int(gust_raw) * kt_to_mph) if gust_raw else None
            result['wind_speed_mph'] = speed_mph

            if direction == 'VRB':
                dir_phrase = 'variable direction'
            else:
                dir_phrase = f"the {deg_to_compass(direction)}"
                result['wind_direction_deg'] = int(direction)

            if speed_raw == 0:
                result['wind_desc'] = 'Calm winds'
            else:
                desc = f"Wind from {dir_phrase} at {speed_mph} mph"
                if gust_mph:
                    desc += f", gusting to {gust_mph} mph"
                result['wind_desc'] = desc
            i += 1

    # Variable wind direction  DDDVDDD
    if i < len(tokens) and re.match(r'^\d{3}V\d{3}$', tokens[i]):
        v_from = deg_to_compass(tokens[i][:3])
        v_to = deg_to_compass(tokens[i][4:])
        if result['wind_desc']:
            result['wind_desc'] += f" (varying {v_from} to {v_to})"
        i += 1

    # Visibility
    if i < len(tokens):
        tok = tokens[i]
        sm_whole = re.match(r'^(\d+)SM$', tok)
        sm_frac = re.match(r'^(\d+)/(\d+)SM$', tok)
        metric = re.match(r'^(\d{4})$', tok)

        if sm_whole:
            v = int(sm_whole.group(1))
            result['visibility_desc'] = 'More than 10 miles' if v >= 10 else f"{v} mile{'s' if v != 1 else ''}"
            i += 1
        elif sm_frac:
            result['visibility_desc'] = f"{sm_frac.group(1)}/{sm_frac.group(2)} mile"
            i += 1
        elif re.match(r'^\d+$', tok) and i + 1 < len(tokens) and re.match(r'^\d+/\d+SM$', tokens[i + 1]):
            result['visibility_desc'] = f"{tok} {tokens[i + 1][:-2]} miles"
            i += 2
        elif metric and not re.match(r'^\d{6}Z$', tok):
            v = int(metric.group(1))
            result['visibility_desc'] = 'More than 10 km' if v == 9999 else f"{v} meters ({round(v / 1609.34, 1)} miles)"
            i += 1

    # Skip RVR entries
    while i < len(tokens) and re.match(r'^R\d+[LCR]?/', tokens[i]):
        i += 1

    # Weather phenomena
    wx_re = re.compile(
        r'^(-|\+|VC)?(MI|BC|PR|DR|BL|SH|TS|FZ)?((?:DZ|RA|SN|SG|IC|PL|GR|GS|UP|BR|FG|FU|VA|DU|SA|HZ|PY|PO|SQ|FC|SS|DS|TS)+)$'
    )
    while i < len(tokens) and wx_re.match(tokens[i]):
        parsed = parse_wx_token(tokens[i])
        if parsed:
            result['weather'].append(parsed)
        i += 1

    # Sky conditions
    sky_base_rank = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ('SKC', 'CLR', 'NSC', 'NCD', 'CAVOK'):
            result['sky_layers'].append(SKY_LABELS[tok])
            i += 1
        elif re.match(r'^(FEW|SCT|BKN|OVC|VV)\d{3}(CB|TCU)?$', tok):
            m = re.match(r'^(FEW|SCT|BKN|OVC|VV)(\d{3})(CB|TCU)?$', tok)
            coverage, height_ft, cb = m.group(1), int(m.group(2)) * 100, m.group(3)
            layer = f"{SKY_LABELS[coverage]} at {height_ft:,} ft"
            if cb == 'CB':
                layer += ' (cumulonimbus — thunderstorm clouds present)'
            elif cb == 'TCU':
                layer += ' (towering cumulus)'
            result['sky_layers'].append(layer)
            rank = SKY_RANK.get(coverage, 0)
            if rank > sky_base_rank:
                sky_base_rank = rank
                result['sky_overall'] = coverage
            i += 1
        else:
            break

    # Temperature / Dewpoint
    if i < len(tokens):
        tm = re.match(r'^(M?\d+)/(M?\d+)?$', tokens[i])
        if tm:
            tc = int(tm.group(1).replace('M', '-'))
            result['temp_c'] = tc
            result['temp_f'] = round(tc * 9 / 5 + 32)
            if tm.group(2):
                dc = int(tm.group(2).replace('M', '-'))
                result['dew_c'] = dc
                result['dew_f'] = round(dc * 9 / 5 + 32)
            i += 1

    # Altimeter
    if i < len(tokens):
        am = re.match(r'^A(\d{4})$', tokens[i])
        qm = re.match(r'^Q(\d{4})$', tokens[i])
        if am:
            result['pressure_inhg'] = int(am.group(1)) / 100
            i += 1
        elif qm:
            hpa = int(qm.group(1))
            result['pressure_inhg'] = round(hpa / 33.8639, 2)
            result['pressure_hpa'] = hpa
            i += 1

    result['headline'] = _build_headline(result)
    return result


def _build_headline(d):
    parts = []

    if d['weather']:
        parts.append(d['weather'][0].capitalize())
    else:
        parts.append(SKY_HEADLINE.get(d.get('sky_overall', 'clear'), 'Partly cloudy'))

    if d['temp_f'] is not None:
        parts.append(f"{d['temp_f']}°F ({d['temp_c']}°C)")

    ws = d.get('wind_speed_mph')
    if ws is not None:
        if ws == 0:
            parts.append('calm winds')
        elif ws < 8:
            parts.append(f"light winds at {ws} mph")
        elif ws < 15:
            parts.append(f"breezy at {ws} mph")
        elif ws < 25:
            parts.append(f"windy at {ws} mph")
        else:
            parts.append(f"very windy at {ws} mph")

    return ', '.join(parts)


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/metar')
def get_metar():
    airport = request.args.get('airport', '').strip().upper()
    if not airport or not re.match(r'^[A-Z0-9]{3,4}$', airport):
        return jsonify({'error': 'Please enter a valid 3- or 4-letter airport code.'}), 400

    try:
        url = f"https://aviationweather.gov/api/data/metar?ids={airport}"
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        raw = resp.text.strip()

        if not raw:
            return jsonify({'error': f"No METAR data found for {airport}. Double-check the airport code."}), 404

        raw = raw.splitlines()[0].strip()
        decoded = decode_metar(raw)
        return jsonify({'success': True, 'data': decoded})

    except requests.exceptions.Timeout:
        return jsonify({'error': 'Request timed out. Please try again.'}), 503
    except requests.exceptions.RequestException as e:
        return jsonify({'error': f"Could not fetch weather data: {e}"}), 503


if __name__ == '__main__':
    app.run(debug=True)
