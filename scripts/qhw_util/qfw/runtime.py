"""QFw runtime helpers used by hardware test workflows."""

from __future__ import annotations


def finish(rc: int = 0) -> int:
	try:
		from defw import me
		me.exit()
	except Exception:
		pass
	return rc
