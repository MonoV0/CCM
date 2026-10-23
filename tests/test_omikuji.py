import importlib
import io
from types import SimpleNamespace as NS

import pytest
from openpyxl import load_workbook
from test_regressions import modules  # noqa: F401


@pytest.mark.parametrize('modules', ['channel_manager'], indirect=True)
def test_matching_and_excel(modules):
    m = importlib.import_module('omikuji')
    assert m.matches(NS(reactions=[NS(emoji=NS(name='Ni_logo2W'), count=1)]))
    assert m.matches(NS(reactions=[NS(emoji=NS(name='Ni_logo1B'), count=1)]))
    assert not m.matches(NS(reactions=[NS(emoji='👍', count=1)]))
    book = load_workbook(io.BytesIO(m.workbook_bytes(
        [(1, '=危険ではなく本文', 'monovo', 'https://discord.com/channels/1/2/3')], ['42'], 10)))
    assert list(book['おみくじ'].values)[1] == (10, '=危険ではなく本文', 'monovo')
    assert book['おみくじ']['B2'].data_type == 's'
    assert '42' in book['使い方・取得結果']['A6'].value


@pytest.mark.asyncio
@pytest.mark.parametrize('modules', ['channel_manager'], indirect=True)
async def test_collection_deduplicates_two_reactions_and_sorts(modules):
    m = importlib.import_module('omikuji')
    def message(mid):
        return NS(id=mid, content='本文', author=NS(name='monovo'), jump_url='url',
                  reactions=[NS(emoji=NS(name=name), count=1) for name in m.EMOJI_NAMES])
    class Channel:
        def history(self, **kwargs):
            assert kwargs == dict(limit=None, oldest_first=True)
            async def stream():
                yield message(2)
                yield message(1)
            return stream()
    progress = {'done': 0}
    rows, errors = await m.collect([Channel()], progress)
    assert [row[0] for row in rows] == [1, 2]
    assert errors == []
    assert progress['done'] == 1
