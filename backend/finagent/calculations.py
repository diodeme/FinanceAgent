"""Pure Decimal calculations over immutable artifacts; no eval, I/O or model calls."""
import ast
from datetime import date
from decimal import Decimal, DecimalException, localcontext
import hashlib
import re

from .contracts import Artifact, CalculateRequest, Cell, FinanceError, FinancialRow, TableData
from .registry import METRICS
from .warnings import artifact_companies, normalize_warnings


class FormulaProblem(Exception):
    pass


def cell_id(company: str, end: date, metric: str) -> str:
    return "c_" + hashlib.sha256(f"{company}|{end}|{metric}".encode()).hexdigest()[:24]


def decimal_text(value: Decimal) -> str:
    if not value.is_finite() or abs(value) > Decimal("1e40"):
        raise FormulaProblem("Non-finite or out-of-bounds numeric result")
    return format(value, "f")


def parse_formula(formula: str) -> ast.Expression:
    if len(formula) > 300:
        raise FinanceError("invalid_formula", "Formula exceeds 300 characters.")
    try:
        tree = ast.parse(formula, mode="eval")
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise FinanceError("invalid_formula", "Formula must be simple arithmetic over metric names.") from exc
    nodes = list(ast.walk(tree))
    allowed = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Name, ast.Load, ast.Constant, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.UAdd, ast.USub)
    if len(nodes) > 64 or any(not isinstance(n, allowed) for n in nodes):
        raise FinanceError("invalid_formula", "Only bounded +, -, *, / arithmetic, parentheses and metric names are allowed.")
    def depth(node):
        return 1 + max((depth(child) for child in ast.iter_child_nodes(node)), default=0)
    if depth(tree) > 12:
        raise FinanceError("invalid_formula", "Formula nesting exceeds 12 levels.")
    for node in nodes:
        if isinstance(node, ast.Constant) and (type(node.value) not in (int, float) or len(ast.get_source_segment(formula, node) or "") > 40):
            raise FinanceError("invalid_formula", "Only finite numeric constants up to 40 characters are allowed.")
        if isinstance(node, ast.Name) and not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", node.id):
            raise FinanceError("invalid_formula", "Invalid metric name in formula.")
    return tree


def formula_names(formula: str) -> set[str]:
    return {node.id for node in ast.walk(parse_formula(formula)) if isinstance(node, ast.Name)}


def _dimensions(unit: str) -> dict[str, int]:
    if unit == "pure":
        return {}
    result = {}
    for term in unit.split("*"):
        match = re.fullmatch(r"([A-Za-z]+)(?:\^(-?[0-9]+))?", term)
        if not match:
            raise FormulaProblem(f"Unsupported unit: {unit}")
        exponent = int(match[2] or 1)
        if abs(exponent) > 12:
            raise FormulaProblem("Unit exponent exceeds limit")
        result[match[1]] = result.get(match[1], 0) + exponent
    return {key: value for key, value in result.items() if value}


def _unit(dimensions: dict[str, int]) -> str:
    return "*".join(key if value == 1 else f"{key}^{value}" for key, value in sorted(dimensions.items()) if value) or "pure"


def evaluate_cell(company: str, end: date, metric: str, label: str, formula: str, cells: dict[str, Cell], expected_unit: str | None = None) -> Cell:
    tree = parse_formula(formula)
    names = sorted({node.id for node in ast.walk(tree) if isinstance(node, ast.Name)})
    dependencies = [cells[name] for name in names if name in cells]
    source_ids = sorted({sid for cell in dependencies for sid in cell.source_ids})
    inputs = [{"cell_id": cell.cell_id, "metric": cell.metric, "value": cell.value, "unit": cell.unit, "source_ids": cell.source_ids, "period_end": end.isoformat()} for cell in dependencies]
    result = Cell(cell_id=cell_id(company, end, metric), metric=metric, label=label, value=None, unit=expected_unit or "pure", status="missing", formula=formula, source_ids=source_ids, inputs=inputs)

    def run(node):
        if isinstance(node, ast.Name):
            cell = cells.get(node.id)
            if cell is None or cell.value is None or cell.status != "available":
                raise FormulaProblem(f"Missing or conflicting input: {node.id}")
            value = Decimal(cell.value)
            decimal_text(value)
            return value, _dimensions(cell.unit)
        if isinstance(node, ast.Constant):
            value = Decimal(ast.get_source_segment(formula, node))
            decimal_text(value)
            return value, {}
        if isinstance(node, ast.UnaryOp):
            value, dimension = run(node.operand)
            return (-value if isinstance(node.op, ast.USub) else value), dimension
        left, ld = run(node.left)
        right, rd = run(node.right)
        if isinstance(node.op, (ast.Add, ast.Sub)):
            if ld != rd:
                raise FormulaProblem("Incompatible units for addition/subtraction")
            value = left + right if isinstance(node.op, ast.Add) else left - right
            dimension = ld
        elif isinstance(node.op, ast.Mult):
            value = left * right
            dimension = {key: ld.get(key, 0) + rd.get(key, 0) for key in ld.keys() | rd.keys()}
        else:
            if right == 0:
                raise FormulaProblem("Zero denominator")
            value = left / right
            dimension = {key: ld.get(key, 0) - rd.get(key, 0) for key in ld.keys() | rd.keys()}
        decimal_text(value)
        if any(abs(power) > 12 for power in dimension.values()):
            raise FormulaProblem("Unit exponent exceeds limit")
        return value, {key: power for key, power in dimension.items() if power}

    try:
        with localcontext() as context:
            context.prec = 34
            value, dimension = run(tree.body)
            unit = _unit(dimension)
            if expected_unit and unit != expected_unit:
                raise FormulaProblem(f"Expected {expected_unit}, got {unit}")
            result.value = decimal_text(value)
            result.unit = unit
            result.status = "available"
    except (FormulaProblem, DecimalException, ValueError) as exc:
        result.reason = str(exc)
        if any(cell.status == "conflicting" for cell in dependencies):
            result.status = "conflicting"
    return result


def populate_metrics(row: FinancialRow, requested: list[str], custom: dict | None = None) -> None:
    """Resolve built-in dependencies; keep inputs in the row for later tools."""
    custom = custom or {}
    # A new custom definition must not silently reuse an old cell with that name.
    cells = {cell.metric: cell for cell in row.cells if cell.metric not in custom}
    visiting = set()
    def resolve(name):
        if name in cells:
            return
        if name in visiting:
            raise FinanceError("invalid_formula", "Cyclic custom metric dependency.")
        visiting.add(name)
        if name in custom:
            spec = custom[name]
            formula, label, unit = spec.formula, spec.label, None
            dependencies = formula_names(formula)
        elif name in METRICS:
            spec = METRICS[name]
            formula, label, unit = spec.formula, spec.label, spec.unit
            dependencies = spec.dependencies
        else:
            raise FinanceError("unsupported_metric", f"Unsupported metric or uncollected custom input: {name}")
        for dependency in dependencies:
            resolve(dependency)
        if formula:
            cells[name] = evaluate_cell(row.company, row.period_end, name, label, formula, cells, unit)
        else:
            cells[name] = Cell(cell_id=cell_id(row.company, row.period_end, name), metric=name, label=label, value=None, unit=unit, status="missing", reason="Input metric was not available in the selected datasets")
        visiting.remove(name)
    for name in requested:
        resolve(name)
    row.cells = list(cells.values())


def _signature(cell: Cell):
    return (cell.value, cell.unit, cell.status, cell.formula, tuple(sorted(cell.source_ids)))


def calculate_data(inputs: list[Artifact], request: CalculateRequest) -> TableData:
    if not inputs or len(inputs) > 8:
        raise FinanceError("invalid_inputs", "Provide between one and eight datasets/tables.")
    if set(request.data_ids) != {artifact.artifact_id for artifact in inputs}:
        raise FinanceError("invalid_inputs", "Loaded artifact IDs do not match the requested data_ids.")
    custom = {spec.name: spec for spec in request.custom_metrics}
    if len(custom) != len(request.custom_metrics) or set(custom) & METRICS.keys():
        raise FinanceError("invalid_formula", "Custom metric names must be unique and must not replace canonical metrics.")
    for spec in custom.values():
        parse_formula(spec.formula)
    for metric in request.metrics:
        if metric not in METRICS and metric not in custom:
            raise FinanceError("unsupported_metric", f"Unsupported metric: {metric}")
    sources, merged, targets, warnings, coverage_missing = {}, {}, set(), [], []
    for artifact in inputs:
        if artifact.kind not in {"dataset", "table"} or artifact.data is None:
            raise FinanceError("invalid_inputs", "Calculations require structured datasets or tables, not narrative text.")
        data = artifact.data
        warnings.extend(artifact.warnings + data.warnings)
        coverage_missing.extend(data.specification.get("coverage_missing", []))
        for sid, source in data.sources.items():
            if sid in sources:
                # Retrieval time and a subsequently assigned DB ID are not fact revisions.
                exclude = {"retrieved_at", "fact_id"}
                if sources[sid].model_dump(exclude=exclude) != source.model_dump(exclude=exclude):
                    raise FinanceError("source_conflict", f"Conflicting evidence under source ID {sid}")
            else:
                sources[sid] = source.model_copy(deep=True)
        targets.update((row.company, row.period_end) for row in data.rows)
        for original in data.rows + data.history_rows:
            key = (original.company, original.period_end)
            if key not in merged:
                merged[key] = original.model_copy(deep=True)
                continue
            row = merged[key]
            if row.period_start != original.period_start:
                raise FinanceError("period_conflict", f"Datasets disagree on the actual fiscal period start for {row.company} ending {row.period_end}.")
            cells = {cell.metric: cell for cell in row.cells}
            for cell in original.cells:
                previous = cells.get(cell.metric)
                # An unobserved, lineage-free gap is not a competing factual version.
                # Filling it enables a follow-up fetch of a previously missing input.
                if previous and previous.status == "missing" and previous.value is None and not previous.source_ids and cell.status == "available":
                    cells[cell.metric] = cell.model_copy(deep=True)
                    continue
                if previous and previous.status == "available" and cell.status == "missing" and cell.value is None and not cell.source_ids:
                    continue
                if previous and _signature(previous) != _signature(cell):
                    cells[cell.metric] = Cell(
                        cell_id=previous.cell_id, metric=cell.metric, label=cell.label, value=None,
                        unit=cell.unit, status="conflicting", source_ids=sorted(set(previous.source_ids + cell.source_ids)),
                        inputs=[{"cell_id": c.cell_id, "value": c.value, "unit": c.unit, "source_ids": c.source_ids} for c in (previous, cell)],
                        reason="Input datasets disagree on value, period, formula or filing vintage; select one version explicitly.",
                    )
                elif not previous:
                    cells[cell.metric] = cell.model_copy(deep=True)
            row.cells = list(cells.values())
    selected = list(dict.fromkeys(request.metrics + list(custom)))
    if not selected:
        selected = list(dict.fromkeys(cell.metric for row in merged.values() for cell in row.cells))
    for row in merged.values():
        recompute = [cell.metric for cell in row.cells if cell.metric in METRICS and METRICS[cell.metric].formula and cell.formula == METRICS[cell.metric].formula and cell.status != "conflicting"]
        row.cells = [cell for cell in row.cells if cell.metric not in recompute]
        # Recompute prior arithmetic from the merged inputs, so a conflicting base
        # fact cannot hide behind an available ratio copied from one old artifact.
        populate_metrics(row, list(dict.fromkeys(selected + recompute)), custom)
    if request.growth != "none":
        original_cells = {key: {cell.metric: cell for cell in row.cells} for key, row in merged.items()}
        for key, row in merged.items():
            lower, upper = (70, 110) if request.growth == "qoq" else (350, 380)
            candidates = [other for other in merged.values() if other.company == row.company and lower <= (row.period_end - other.period_end).days <= upper and (not row.period_start or not other.period_start or abs((row.period_end-row.period_start).days - (other.period_end-other.period_start).days) <= 14)]
            prior = candidates[0] if len(candidates) == 1 else None
            for metric in selected:
                current = original_cells[key][metric]
                previous = original_cells[(prior.company, prior.period_end)].get(metric) if prior else None
                name = f"{metric}_{request.growth}"
                operands = {"current": current}
                if previous:
                    operands["prior"] = previous
                result = evaluate_cell(row.company, row.period_end, name, f"{current.label} {request.growth.upper()}", "(current - prior) / prior", operands, "pure")
                # Store actual prior dates, not the current date attached by generic arithmetic.
                for item in result.inputs:
                    if previous and item["cell_id"] == previous.cell_id:
                        item["period_end"] = prior.period_end.isoformat()
                if prior is None:
                    result.reason = f"No unique comparable {request.growth} prior quarter in inputs/history"
                row.cells.append(result)
    ordered = sorted(merged.values(), key=lambda row: (row.company, row.period_end), reverse=True)
    return TableData(
        rows=[row for row in ordered if (row.company, row.period_end) in targets],
        history_rows=[row for row in ordered if (row.company, row.period_end) not in targets], sources=sources,
        specification={"operation": "calculate", **request.model_dump(mode="json"), "requested_metrics": selected + ([f"{metric}_{request.growth}" for metric in selected] if request.growth != "none" else []), "coverage_missing": list(dict.fromkeys(coverage_missing))},
        warnings=normalize_warnings(warnings, companies=artifact_companies(inputs)),
        selection_policy="explicit-input-snapshots-v1",
    )
