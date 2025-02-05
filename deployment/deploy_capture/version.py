"""
(*)~---------------------------------------------------------------------------
Pupil - eye tracking platform
Copyright (C) 2012-2022 Pupil Labs

Distributed under the terms of the GNU
Lesser General Public License (LGPL v3.0).
See COPYING and COPYING.LESSER for license details.
---------------------------------------------------------------------------~(*)
"""
import os
import sys

sys.path.append(os.path.join("../../", "pupil_src", "shared_modules"))
from version_utils import get_tag_commit, pupil_version, write_version_file
