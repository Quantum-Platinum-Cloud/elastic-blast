#                           PUBLIC DOMAIN NOTICE
#              National Center for Biotechnology Information
#  
# This software is a "United States Government Work" under the
# terms of the United States Copyright Act.  It was written as part of
# the authors' official duties as United States Government employees and
# thus cannot be copyrighted.  This software is freely available
# to the public for use.  The National Library of Medicine and the U.S.
# Government have not placed any restriction on its use or reproduction.
#   
# Although all reasonable efforts have been taken to ensure the accuracy
# and reliability of the software and data, the NLM and the U.S.
# Government do not and cannot warrant the performance or results that
# may be obtained by using this software or data.  The NLM and the U.S.
# Government disclaim all warranties, express or implied, including
# warranties of performance, merchantability or fitness for any particular
# purpose.
#   
# Please cite NCBI in any work or product based on this material.

"""
Unit tests for filehelper module

Author: Victor Joukov joukovv@ncbi.nlm.nih.gov
"""

import os
from elb import filehelper

TEST_DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')

expected = """\
Test file 1
Another line
Test file 2
Waxing poetic
"""


def test_tar_merge_read():
    with filehelper.open_for_read(os.path.join(TEST_DATA_DIR, 'test.tar')) as f:
        contents = f.read()
        assert(contents == expected)
