"""Fail-closed MySQL static screening before any EXPLAIN request."""

from __future__ import annotations

import re


class SQLValidationError(ValueError):
    pass


def validate_sql(sql: str, database: str, whitelist: dict[str, list[str]], *, controls: bool = False) -> dict:
    try:
        return _validate_sql(sql, database, whitelist, controls=controls)
    except SQLValidationError:
        raise
    except ImportError as exc:
        raise SQLValidationError("Install requirements-planner.txt for MySQL AST validation") from exc
    except Exception as exc:
        raise SQLValidationError(str(exc)) from exc


def _validate_sql(sql, database, whitelist, *, controls):
    import sqlglot
    from sqlglot import exp
    from sqlglot.errors import ErrorLevel
    from sqlglot.optimizer.qualify import qualify
    from sqlglot.optimizer.scope import traverse_scope

    text = sql.strip().removesuffix(";").strip()
    if controls and text.upper() in {"BEGIN", "COMMIT", "ROLLBACK", "UNLOCK TABLES"}:
        return {"explain": False, "tables": [], "validation": "transaction_control"}
    lock = re.fullmatch(r"LOCK TABLES `?([A-Za-z_][A-Za-z0-9_]*)`? (READ|WRITE)", text, re.I)
    if controls and lock:
        if lock[1] not in whitelist:
            raise SQLValidationError("LOCK TABLES target is not in identifier whitelist")
        return {"explain": False, "tables": [lock[1]], "validation": "table_lock_control"}
    trees = sqlglot.parse(text, read="mysql", error_level=ErrorLevel.RAISE)
    if len(trees) != 1 or trees[0] is None:
        raise SQLValidationError("Exactly one SQL statement is required")
    tree = trees[0]
    if any(n.comments for n in tree.walk()):
        raise SQLValidationError("SQL comments and executable comments are unsupported")
    if not isinstance(tree, (exp.Select, exp.Union, exp.Update, exp.Delete, exp.Insert)):
        raise SQLValidationError("Unsupported SQL statement; DDL, SET, CALL and arbitrary commands are prohibited")
    for node in tree.walk():
        if isinstance(node, (exp.Into, exp.Command, exp.Parameter, exp.SessionParameter)):
            raise SQLValidationError("SQL file output, variables and commands are prohibited")
        if isinstance(node, exp.Anonymous):
            raise SQLValidationError(f"Unapproved SQL function: {node.name}")
        if isinstance(node, exp.Table) and (node.catalog or (node.db and node.db != database)):
            raise SQLValidationError("Cross-database table reference is prohibited")
        if isinstance(node, exp.Column) and (node.catalog or (node.db and node.db != database)):
            raise SQLValidationError("Cross-database column reference is prohibited")
    query = tree.copy()
    target_tables = []
    if isinstance(tree, (exp.Update, exp.Delete)):
        if not isinstance(tree.this, exp.Table) or tree.this.args.get("joins") or tree.args.get("using") or tree.args.get("from_") or tree.args.get("with_"):
            raise SQLValidationError("Only single-table UPDATE/DELETE is supported")
        if not tree.args.get("where") or not tree.args["where"].find(exp.Column):
            raise SQLValidationError("UPDATE/DELETE requires a column-based WHERE predicate")
        columns = list(tree.find_all(exp.Column))
        query = exp.select(*(c.copy() for c in columns)).from_(tree.this.copy())
        query.set("where", tree.args["where"].copy())
        if any(tree.find_all(exp.Subquery)):
            raise SQLValidationError("DML subqueries require a dedicated compiler strategy")
    elif isinstance(tree, exp.Insert):
        if not isinstance(tree.this, exp.Schema) or not isinstance(tree.this.this, exp.Table):
            raise SQLValidationError("INSERT requires explicit target columns")
        target = tree.this.this.name
        target_tables.append(target)
        if target not in whitelist:
            raise SQLValidationError(f"Table not in identifier whitelist: {target}")
        if tree.args.get("conflict"):
            raise SQLValidationError("INSERT conflict clauses are unsupported")
        for column in tree.this.expressions:
            if column.name.lower() not in {c.lower() for c in whitelist[target]}:
                raise SQLValidationError(f"Unknown INSERT column: {column.name}")
        query = tree.expression.copy()
        if isinstance(query, exp.Values):
            for row in query.expressions:
                if len(row.expressions) != len(tree.this.expressions) or any(
                    not isinstance(item, (exp.Literal, exp.Null, exp.Boolean)) for item in row.expressions
                ):
                    raise SQLValidationError("INSERT VALUES requires matching literal values")
            return {"explain": True, "tables": target_tables, "validation": "mysql_ast"}
        if not isinstance(query, (exp.Select, exp.Union)):
            raise SQLValidationError("Unsupported INSERT source")
    physical_tables = set(target_tables)
    for scope in traverse_scope(query):
        for _, source in scope.selected_sources.values():
            if isinstance(source, exp.Table):
                if source.name not in whitelist:
                    raise SQLValidationError(f"Table not in identifier whitelist: {source.name}")
                if not whitelist[source.name]:
                    raise SQLValidationError(f"Column schema unavailable for table: {source.name}")
                physical_tables.add(source.name)
    schema = {database: {table: {col: "UNKNOWN" for col in cols} for table, cols in whitelist.items()}}
    qualify(query, dialect="mysql", db=database, schema=schema, infer_schema=False,
            validate_qualify_columns=True, allow_partial_qualification=False)
    return {"explain": True, "tables": sorted(physical_tables), "validation": "mysql_ast"}
