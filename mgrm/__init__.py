"""MacGear Rebate Master.

The single place every customer rebate agreement and rate is held, together
with the registers they depend on: customers, customer groups and brands
(spec §4.5; split from the forecasting platform on 8 Oct 2026). The
forecasting platform reads all of these through a read-only feed (mgrm.api)
and never edits them.

Layers: a layer may import the layers below it and nothing above.

    7  mgrm.web, mgrm.api, mgrm.__main__   screens, the feed, the command line
    2  mgrm.rebates                        agreements, rates, evidence, review
    1  mgrm.data                           NetSuite registers, legacy workbook load

Shared, layer-free: mgrm.domain, mgrm.checks, mgrm.config, mgrm.db, mgrm.models, mgrm.auth
"""
