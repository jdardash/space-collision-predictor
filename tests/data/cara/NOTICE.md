# NASA CARA test fixtures

Source repository: https://github.com/nasa/CARA_Analysis_Tools
Pinned commit: 86ba28a
License: NASA Open Source Agreement (NOSA) v1.3 (full text in NOSA_GSC-18593-1.pdf)

Copyright (c) United States Government as represented by the Administrator
of the National Aeronautics and Space Administration. All Rights Reserved.

The NOSA v1.3 permits use, reproduction, modification, distribution and
redistribution of the Original Software provided the agreement text and this
notice travel with it. Nothing in this directory has been modified apart from
the xlsx-to-CSV conversion and the JSON extraction of MATLAB literals noted
below; the CDM and PDF bytes are verbatim copies.

The four NOSA agreements published at the root of the source repository are
NOSA_GSC-18593-1.pdf, NOSA_GSC-18848-1.pdf, NOSA_GSC-19027-1.pdf, NOSA_GSC-19374-1.pdf. One of them (NOSA_GSC-18593-1.pdf) is
vendored here; the others are identical in terms and differ only in the NASA
case number they cover.

## Files

| Path | Count | Source in CARA_Analysis_Tools |
| --- | ---: | --- |
| `cdms/*.cdm` | 53 | `DataFiles/PcTestCaseCDMs/*.cdm` |
| `sample_cdms/*.cdm` | 34 | `DataFiles/SampleCDMs/*.cdm` |
| `pc_method_test_conjunctions.csv` | 1 | `DataFiles/PcTestCaseCDMs/CARA_PcMethod_Test_Conjunctions.xlsx` (converted) |
| `alfano_expected_pc.json` | 1 | `DistributedMatlab/ProbabilityOfCollision/UnitTests/Pc2D_Foster_UnitTest.m` lines 31-45 (extracted) |
| `alfano_pc3d_reference.csv` | 1 | `DistributedMatlab/ProbabilityOfCollision/Pc3D_Hall_Utils/Output_AlfanoTestCases_Pc3D/A09TestCases_01_12_Distributed.csv` |
| `NOSA_GSC-18593-1.pdf` | 1 | repository root |

Total vendored size: 949 KiB.

## Regenerating

```bash
.venv/Scripts/python scripts/import_cara_fixtures.py
```

The importer is the only thing that writes this directory. Do not hand-edit
the fixtures; re-run the importer against the pinned clone instead.
