"""The one-time move of the registers and the rebate master out of the forecasting platform (8 Oct 2026).

Copies, keeping every record's id: users, customer groups, customers, brands,
the loads they came from, agreements, rates, true-ups, brand approvers, the
legacy workbook months, and the audit history of all of these. Then checks
every table: the same number of rows and the same ids as the source. Nothing
is saved unless every table matches. The source is only read.
"""

from dataclasses import dataclass, field

from sqlalchemy import MetaData, Table, create_engine, func, insert, select, text
from sqlalchemy.orm import Session, sessionmaker

from mgrm.models import (
    AppUser,
    AuditEvent,
    Brand,
    Customer,
    CustomerGroup,
    LoadBatch,
    LoadKind,
    RebateAgreement,
    RebateApproverScope,
    RebateRate,
    RebateTrueUp,
    RebateWorkbookMonth,
)

# Forecasting-platform roles -> rebate master roles.
ROLE_MAP = {
    "admin": "admin", "finance_editor": "rebate_editor", "rebate_reviewer": "rebate_reviewer",
    "brand_approver": "brand_approver", "viewer": "viewer", "rebates_only": "viewer",
}
LOAD_KINDS_MOVED = ("customers", "classes", "rebate_workbook")
AUDIT_PREFIXES = ("rebate.", "brand.", "customer.", "customer_group.")

# (target model, source table, filter on the source) in an order that respects foreign keys.
PLAN = [
    (AppUser, "app_user", None),
    (CustomerGroup, "customer_group", None),
    (Brand, "brand", None),
    (Customer, "customer", None),
    (LoadBatch, "load_batch", "kind IN ('customers', 'classes', 'rebate_workbook')"),
    (RebateAgreement, "rebate_agreement", None),
    (RebateRate, "rebate_rate", None),
    (RebateTrueUp, "rebate_true_up", None),
    (RebateApproverScope, "rebate_approver_scope", None),
    (RebateWorkbookMonth, "rebate_workbook_month", None),
    (AuditEvent, "audit_event",
     "action LIKE 'rebate.%' OR action LIKE 'brand.%' OR action LIKE 'customer.%' OR action LIKE 'customer_group.%' "
     "OR (action = 'data.load' AND detail->>'kind' IN ('customers', 'classes', 'rebate_workbook'))"),
]
KEYED_BY_CODE = {"customer_group"}  # primary key is a code, not an id


@dataclass
class ImportReport:
    tables: dict[str, tuple[int, int]] = field(default_factory=dict)  # name -> (source rows, target rows)
    problems: list[str] = field(default_factory=list)

    @property
    def verified(self) -> bool:
        return not self.problems

    def __str__(self) -> str:
        lines = [f"  {name:24} source {src:6}  copied {dst:6}" for name, (src, dst) in self.tables.items()]
        verdict = "VERIFIED and saved." if self.verified else "NOT saved. Problems:\n    " + "\n    ".join(self.problems)
        return "Import from the forecasting platform\n" + "\n".join(lines) + f"\n{verdict}"


def import_all(*, source_url: str, target_factory: sessionmaker[Session]) -> ImportReport:
    report = ImportReport()
    source_engine = create_engine(source_url)
    source_meta = MetaData()
    with source_engine.connect() as source, target_factory() as target:
        if target.scalar(select(func.count()).select_from(RebateAgreement)):
            report.problems.append("The rebate master already holds agreements: the import has been done before.")
            return report
        for model, table_name, where in PLAN:
            table = Table(table_name, source_meta, autoload_with=source)
            query = select(table)
            if where:
                query = query.where(text(where))
            rows = [dict(r._mapping) for r in source.execute(query)]
            columns = set(model.__table__.columns.keys())
            records = []
            for row in rows:
                record = {k: v for k, v in row.items() if k in columns}
                if table_name == "app_user":
                    record["role"] = ROLE_MAP[record["role"]]
                records.append(record)
            if table_name in KEYED_BY_CODE:
                existing = set(target.scalars(select(model.code)))
                records = [r for r in records if r["code"] not in existing]
            if records:
                target.execute(insert(model.__table__), records)
            key = "code" if table_name in KEYED_BY_CODE else "id"
            source_keys = sorted(str(r[key]) for r in rows)
            target_keys = sorted(str(k) for k in target.scalars(select(model.__table__.c[key])))
            report.tables[table_name] = (len(source_keys), len(target_keys))
            missing = set(source_keys) - set(target_keys)
            if missing:
                report.problems.append(f"{table_name}: {len(missing)} rows did not arrive, e.g. {sorted(missing)[:3]}")
            if table_name not in KEYED_BY_CODE and len(target_keys) != len(source_keys):
                report.problems.append(f"{table_name}: {len(source_keys)} rows in the source but {len(target_keys)} here")
        if report.problems:
            target.rollback()
            return report
        for model, table_name, _ in PLAN:
            if table_name not in KEYED_BY_CODE:
                target.execute(text(
                    f"SELECT setval(pg_get_serial_sequence('{table_name}', 'id'), "
                    f"COALESCE((SELECT MAX(id) FROM {table_name}), 0) + 1, false)"
                ))
        target.add(LoadBatch(kind=LoadKind.FORECASTING_IMPORT, entity_code=None, source="forecasting platform database",
                             file_name="(database to database)", file_sha256="-", row_count=sum(d for _, d in report.tables.values()),
                             summary={name: {"source": s, "copied": d} for name, (s, d) in report.tables.items()}))
        target.add(AuditEvent(action="data.forecasting_import", subject="rebate master",
                              detail={name: d for name, (_, d) in report.tables.items()}))
        target.commit()
    source_engine.dispose()
    return report
