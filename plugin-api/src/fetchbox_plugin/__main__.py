"""``python -m fetchbox_plugin check <path>``: the same check as ``fetchbox plugin check``."""

import sys

from fetchbox_plugin.check import main

sys.exit(main())
