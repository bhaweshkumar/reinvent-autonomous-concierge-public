from unittest.mock import AsyncMock

from reinvent_concierge.booking.reader import ScheduleReader
from tests.factories import make_schedule, make_session


async def test_reader_always_passes_the_local_resolver():
    source = AsyncMock()
    source.get_schedule = AsyncMock(return_value=make_schedule(make_session("A")))
    resolver = AsyncMock(return_value=None)
    reader = ScheduleReader(source, resolver)

    await reader.read(include_favorites=False)

    source.get_schedule.assert_awaited_once_with(
        include_favorites=False, include_personal_time=True, session_resolver=resolver
    )
