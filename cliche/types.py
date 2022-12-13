from datetime import datetime, date


class DateStr(date):
    def __new__(_cls, date_str: str) -> date:  # type: ignore # pylint: disable=signature-differs
        return datetime.strptime(date_str, "%Y-%m-%d").date()


class DateTimeStr(datetime):
    def __new__(_cls, date_str: str) -> datetime:  # type: ignore # pylint: disable=signature-differs
        return datetime.fromisoformat(date_str)
