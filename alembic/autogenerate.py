"""Provide the 'autogenerate' feature which can produce migration operations
automatically."""

from alembic.context import _context_opts, get_bind
from alembic import util
from sqlalchemy.engine.reflection import Inspector
from sqlalchemy import types as sqltypes, schema
import re

import logging
log = logging.getLogger(__name__)

###################################################
# top level

def produce_migration_diffs(template_args, imports):
    metadata = _context_opts['autogenerate_metadata']
    if metadata is None:
        raise util.CommandError(
                "Can't proceed with --autogenerate option; environment "
                "script env.py does not provide "
                "a MetaData object to the context.")
    connection = get_bind()
    diffs = []
    _produce_net_changes(connection, metadata, diffs)
    _set_upgrade(template_args, _indent(_produce_upgrade_commands(diffs, imports)))
    _set_downgrade(template_args, _indent(_produce_downgrade_commands(diffs, imports)))
    template_args['imports'] = "\n".join(sorted(imports))

def _set_upgrade(template_args, text):
    template_args[_context_opts['upgrade_token']] = text

def _set_downgrade(template_args, text):
    template_args[_context_opts['downgrade_token']] = text

def _indent(text):
    text = "### commands auto generated by Alembic - please adjust! ###\n" + text
    text += "\n### end Alembic commands ###"
    text = re.compile(r'^', re.M).sub("    ", text).strip()
    return text

###################################################
# walk structures

def _produce_net_changes(connection, metadata, diffs):
    inspector = Inspector.from_engine(connection)
    # TODO: not hardcode alembic_version here ?
    conn_table_names = set(inspector.get_table_names()).\
                            difference(['alembic_version'])
    metadata_table_names = set(metadata.tables)

    for tname in metadata_table_names.difference(conn_table_names):
        diffs.append(("add_table", metadata.tables[tname]))
        log.info("Detected added table %r", tname)

    removal_metadata = schema.MetaData()
    for tname in conn_table_names.difference(metadata_table_names):
        t = schema.Table(tname, removal_metadata)
        inspector.reflecttable(t, None)
        diffs.append(("remove_table", t))
        log.info("Detected removed table %r", tname)

    existing_tables = conn_table_names.intersection(metadata_table_names)

    conn_column_info = dict(
        (tname, 
            dict(
                (rec["name"], rec)
                for rec in inspector.get_columns(tname)
            )
        )
        for tname in existing_tables
    )

    for tname in existing_tables:
        _compare_columns(tname, 
                conn_column_info[tname], 
                metadata.tables[tname],
                diffs)

    # TODO: 
    # index add/drop
    # table constraints
    # sequences

###################################################
# element comparison

def _compare_columns(tname, conn_table, metadata_table, diffs):
    metadata_cols_by_name = dict((c.name, c) for c in metadata_table.c)
    conn_col_names = set(conn_table)
    metadata_col_names = set(metadata_cols_by_name)

    for cname in metadata_col_names.difference(conn_col_names):
        diffs.append(
            ("add_column", tname, metadata_cols_by_name[cname])
        )
        log.info("Detected added column '%s.%s'", tname, cname)

    for cname in conn_col_names.difference(metadata_col_names):
        diffs.append(
            ("remove_column", tname, schema.Column(
                cname,
                conn_table[cname]['type'],
                nullable=conn_table[cname]['nullable'],
                server_default=conn_table[cname]['default']
            ))
        )
        log.info("Detected removed column '%s.%s'", tname, cname)

    for colname in metadata_col_names.intersection(conn_col_names):
        metadata_col = metadata_table.c[colname]
        conn_col = conn_table[colname]
        col_diff = []
        _compare_type(tname, colname,
            conn_col,
            metadata_col.type,
            col_diff
        )
        _compare_nullable(tname, colname,
            conn_col,
            metadata_col.nullable,
            col_diff
        )
        _compare_server_default(tname, colname,
            conn_col,
            metadata_col.server_default,
            col_diff
        )
        if col_diff:
            diffs.append(col_diff)

def _compare_nullable(tname, cname, conn_col, 
                            metadata_col_nullable, diffs):
    conn_col_nullable = conn_col['nullable']
    if conn_col_nullable is not metadata_col_nullable:
        diffs.append(
            ("modify_nullable", tname, cname, 
                {
                    "existing_type":conn_col['type'],
                    "existing_server_default":conn_col['default'],
                },
                conn_col_nullable, 
                metadata_col_nullable),
        )
        log.info("Detected %s on column '%s.%s'", 
            "NULL" if metadata_col_nullable else "NOT NULL",
            tname,
            cname
        )

def _compare_type(tname, cname, conn_col, metadata_type, diffs):
    conn_type = conn_col['type']
    if conn_type._compare_type_affinity(metadata_type):
        comparator = _type_comparators.get(conn_type._type_affinity, None)

        isdiff = comparator and comparator(metadata_type, conn_type)
    else:
        isdiff = True

    if isdiff:
        diffs.append(
            ("modify_type", tname, cname, 
                    {
                        "existing_nullable":conn_col['nullable'],
                        "existing_server_default":conn_col['default'],
                    },
                    conn_type, 
                    metadata_type),
        )
        log.info("Detected type change from %r to %r on '%s.%s'", 
            conn_type, metadata_type, tname, cname
        )

def _compare_server_default(tname, cname, conn_col, metadata_default, diffs):
    conn_col_default = conn_col['default']
    rendered_metadata_default = _render_server_default(metadata_default)
    if conn_col_default != rendered_metadata_default:
        diffs.append(
            ("modify_default", tname, cname, 
                {
                    "existing_nullable":conn_col['nullable'],
                    "existing_type":conn_col['type'],
                },
                conn_col_default,
                metadata_default),
        )
        log.info("Detected server default on column '%s.%s'", 
            tname,
            cname
        )

def _string_compare(t1, t2):
    return \
        t1.length is not None and \
        t1.length != t2.length

def _numeric_compare(t1, t2):
    return \
        (
            t1.precision is not None and \
            t1.precision != t2.precision
        ) or \
        (
            t1.scale is not None and \
            t1.scale != t2.scale
        )
_type_comparators = {
    sqltypes.String:_string_compare,
    sqltypes.Numeric:_numeric_compare
}

###################################################
# produce command structure

def _produce_upgrade_commands(diffs, imports):
    buf = []
    for diff in diffs:
        buf.append(_invoke_command("upgrade", diff, imports))
    return "\n".join(buf)

def _produce_downgrade_commands(diffs, imports):
    buf = []
    for diff in diffs:
        buf.append(_invoke_command("downgrade", diff, imports))
    return "\n".join(buf)

def _invoke_command(updown, args, imports):
    if isinstance(args, tuple):
        return _invoke_adddrop_command(updown, args, imports)
    else:
        return _invoke_modify_command(updown, args, imports)

def _invoke_adddrop_command(updown, args, imports):
    cmd_type = args[0]
    adddrop, cmd_type = cmd_type.split("_")

    cmd_args = args[1:] + (imports,)

    _commands = {
        "table":(_drop_table, _add_table),
        "column":(_drop_column, _add_column),
    }

    cmd_callables = _commands[cmd_type]

    if (
        updown == "upgrade" and adddrop == "add"
    ) or (
        updown == "downgrade" and adddrop == "remove"
    ):
        return cmd_callables[1](*cmd_args)
    else:
        return cmd_callables[0](*cmd_args)

def _invoke_modify_command(updown, args, imports):
    tname, cname = args[0][1:3]
    kw = {}

    _arg_struct = {
        "modify_type":("existing_type", "type_"),
        "modify_nullable":("existing_nullable", "nullable"),
        "modify_default":("existing_server_default", "server_default"),
    }
    for diff in args:
        diff_kw = diff[3]
        for arg in ("existing_type", \
                "existing_nullable", \
                "existing_server_default"):
            if arg in diff_kw:
                kw.setdefault(arg, diff_kw[arg])
        old_kw, new_kw = _arg_struct[diff[0]]
        if updown == "upgrade":
            kw[new_kw] = diff[-1]
            kw[old_kw] = diff[-2]
        else:
            kw[new_kw] = diff[-2]
            kw[old_kw] = diff[-1]

    if "nullable" in kw:
        kw.pop("existing_nullable", None)
    if "server_default" in kw:
        kw.pop("existing_server_default", None)
    return _modify_col(tname, cname, imports, **kw)

###################################################
# render python

def _add_table(table, imports):
    return "create_table(%(tablename)r,\n%(args)s\n)" % {
        'tablename':table.name,
        'args':',\n'.join(
            [_render_column(col, imports) for col in table.c] +
            sorted([rcons for rcons in 
                [_render_constraint(cons) for cons in 
                    table.constraints]
                if rcons is not None
            ])
        ),
    }

def _drop_table(table, imports):
    return "drop_table(%r)" % table.name

def _add_column(tname, column, imports):
    return "add_column(%r, %s)" % (
            tname, 
            _render_column(column, imports))

def _drop_column(tname, column, imports):
    return "drop_column(%r, %r)" % (tname, column.name)

def _modify_col(tname, cname, 
                imports,
                server_default=False,
                type_=None,
                nullable=None,
                existing_type=None,
                existing_nullable=None,
                existing_server_default=False):
    prefix = _autogenerate_prefix()
    indent = " " * 11
    text = "alter_column(%r, %r" % (tname, cname)
    text += ", \n%sexisting_type=%s" % (indent, 
                    _repr_type(prefix, existing_type, imports))
    if server_default is not False:
        text += ", \n%sserver_default=%s" % (indent, 
                        _render_server_default(server_default),)
    if type_ is not None:
        text += ", \n%stype_=%s" % (indent, _repr_type(prefix, type_, imports))
    if nullable is not None:
        text += ", \n%snullable=%r" % (
                        indent, nullable,)
    if existing_nullable is not None:
        text += ", \n%sexisting_nullable=%r" % (
                        indent, existing_nullable)
    if existing_server_default:
        text += ", \n%sexisting_server_default=%s" % (
                        indent, 
                        _render_server_default(
                            existing_server_default),
                    )
    text += ")"
    return text

def _autogenerate_prefix():
    return _context_opts['autogenerate_sqlalchemy_prefix']

def _render_column(column, imports):
    opts = []
    if column.server_default:
        opts.append(("server_default", 
                    _render_server_default(column.server_default)))
    if column.nullable is not None:
        opts.append(("nullable", column.nullable))

    # TODO: for non-ascii colname, assign a "key"
    return "%(prefix)sColumn(%(name)r, %(type)s, %(kw)s)" % {
        'prefix':_autogenerate_prefix(),
        'name':column.name,
        'type':_repr_type(_autogenerate_prefix(), column.type, imports),
        'kw':", ".join(["%s=%s" % (kwname, val) for kwname, val in opts])
    }

def _render_server_default(default):
    if isinstance(default, schema.DefaultClause):
        if isinstance(default.arg, basestring):
            default = default.arg
        else:
            default = str(default.arg)
    if isinstance(default, basestring):
        # TODO: this is just a hack to get 
        # tests to pass until we figure out
        # WTF sqlite is doing
        default = default.replace("'", "")
        return "'%s'" % default
    else:
        return None

def _repr_type(prefix, type_, imports):
    mod = type(type_).__module__
    if mod.startswith("sqlalchemy.dialects"):
        dname = re.match(r"sqlalchemy\.dialects\.(\w+)", mod).group(1)
        imports.add("from sqlalchemy.dialects import %s" % dname)
        return "%s.%r" % (dname, type_)
    else:
        return "%s%r" % (prefix, type_)

def _render_constraint(constraint):
    renderer = _constraint_renderers.get(type(constraint), None)
    if renderer:
        return renderer(constraint)
    else:
        return None

def _render_primary_key(constraint):
    opts = []
    if constraint.name:
        opts.append(("name", repr(constraint.name)))
    return "%(prefix)sPrimaryKeyConstraint(%(args)s)" % {
        "prefix":_autogenerate_prefix(),
        "args":", ".join(
            [repr(c.key) for c in constraint.columns] +
            ["%s=%s" % (kwname, val) for kwname, val in opts]
        ),
    }

def _render_foreign_key(constraint):
    opts = []
    if constraint.name:
        opts.append(("name", repr(constraint.name)))
    # TODO: deferrable, initially, etc.
    return "%(prefix)sForeignKeyConstraint([%(cols)s], [%(refcols)s], %(args)s)" % {
        "prefix":_autogenerate_prefix(),
        "cols":", ".join(f.parent.key for f in constraint.elements),
        "refcols":", ".join(repr(f._get_colspec()) for f in constraint.elements),
        "args":", ".join(
            ["%s=%s" % (kwname, val) for kwname, val in opts]
        ),
    }

def _render_check_constraint(constraint):
    opts = []
    if constraint.name:
        opts.append(("name", repr(constraint.name)))
    return "%(prefix)sCheckConstraint('TODO')" % {
            "prefix":_autogenerate_prefix()
        }

_constraint_renderers = {
    schema.PrimaryKeyConstraint:_render_primary_key,
    schema.ForeignKeyConstraint:_render_foreign_key,
    schema.CheckConstraint:_render_check_constraint
}
