import json
from pathlib import Path


def test_kuaishou_raw_cookie_has_optional_configuration_field():
    schema = json.loads((Path(__file__).parents[1] / '_conf_schema.json').read_text())
    field = schema['cookies']['items'].get('kuaishou_cookie')
    assert field is not None, 'Kuaishou comments need an optional raw Cookie field'
    assert field['type'] == 'text'
    assert field['default'] == ''
    assert '风控' in field['hint']
