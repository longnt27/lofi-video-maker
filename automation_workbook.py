"""Read the existing LoFi queue without changing its native checkbox XML."""
from pathlib import Path, PurePosixPath
import posixpath
import re
import xml.etree.ElementTree as ET
from zipfile import ZipFile

NS = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main',
      'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'}


def read_sheets(path: Path) -> dict:
    with ZipFile(path) as book:
        if sum(item.file_size for item in book.infolist()) > 80_000_000:
            raise ValueError('Workbook exceeds the 80 MB uncompressed limit')
        strings = []
        if 'xl/sharedStrings.xml' in book.namelist():
            strings = [''.join(item.itertext()) for item in ET.fromstring(book.read('xl/sharedStrings.xml'))]
        links = {item.attrib['Id']: item.attrib['Target'] for item in ET.fromstring(book.read('xl/_rels/workbook.xml.rels'))}
        result = {}
        for sheet in ET.fromstring(book.read('xl/workbook.xml')).findall('s:sheets/s:sheet', NS):
            target = links[sheet.attrib['{' + NS['r'] + '}id']]
            member = target.lstrip('/') if target.startswith('/') else posixpath.normpath('xl/' + target)
            if not member.startswith('xl/') or '..' in PurePosixPath(member).parts:
                raise ValueError('Invalid worksheet path')
            rows = {}
            for row in ET.fromstring(book.read(member)).findall('s:sheetData/s:row', NS):
                cells = {}
                for cell in row.findall('s:c', NS):
                    column = re.match(r'[A-Z]+', cell.attrib['r'])[0]
                    value = cell.findtext('s:v', '', NS)
                    kind = cell.attrib.get('t')
                    if kind == 's': value = strings[int(value)]
                    elif kind == 'inlineStr': value = ''.join(cell.find('s:is', NS).itertext())
                    elif kind == 'b': value = value == '1'
                    cells[column] = value
                rows[int(row.attrib['r'])] = cells
            result[sheet.attrib['name']] = rows
        return result


def queue_records(path: Path) -> list[dict]:
    sheets = read_sheets(path)
    required = {'Kich_ban', 'Prompts', 'YouTube'}
    if not required.issubset(sheets):
        raise ValueError('Workbook requires Kich_ban, Prompts and YouTube sheets')
    def indexed(name):
        result = {}
        for row, cells in sheets[name].items():
            key = str(cells.get('A', '')).strip()
            if re.fullmatch(r'LOFI-\d{3,}', key):
                if key in result: raise ValueError(f'Duplicate script ID in {name}: {key}')
                result[key] = (row, cells)
        return result
    prompts, youtube = indexed('Prompts'), indexed('YouTube')
    output = []
    for key, (row, cells) in indexed('Kich_ban').items():
        done = cells.get('I', False)
        if done is True or str(done).upper() in ('TRUE', '1'): continue
        if str(cells.get('H', '')).strip() == 'BO_QUA': continue
        if key not in prompts or key not in youtube: raise ValueError(f'Missing prompt or YouTube row: {key}')
        p, y = prompts[key][1], youtube[key][1]
        if any(not str(p.get(column, '')).strip() for column in ('B', 'D', 'E')):
            raise ValueError(f'Missing image/animation/music prompt: {key}')
        output.append({'id': key, 'row': row, 'checkbox_cell': f'I{row}', 'sheet': 'Kich_ban',
            'title': str(cells.get('B') or key), 'image_prompt_1': p['B'],
            'animation_prompt': p['D'], 'music_prompt': p['E'], 'track_title': p.get('F', key),
            'youtube': dict(zip(('title', 'description', 'tags', 'hashtags'), (y.get(c, '') for c in 'BCDE')))})
    if not output and not indexed('Kich_ban'):
        raise ValueError('No LOFI IDs found in workbook')
    return output
