"""Rewrite assertion AST to produce nice error messages"""

import ast
import collections
import itertools
import imp
import marshal
import os
import struct
import sys
import types

import py
from _pytest.assertion import util


# py.test caches rewritten pycs in __pycache__.
if hasattr(imp, "get_tag"):
    PYTEST_TAG = imp.get_tag() + "-PYTEST"
else:
    if hasattr(sys, "pypy_version_info"):
        impl = "pypy"
    elif sys.platform == "java":
        impl = "jython"
    else:
        impl = "cpython"
    ver = sys.version_info
    PYTEST_TAG = "%s-%s%s-PYTEST" % (impl, ver[0], ver[1])
    del ver, impl

class AssertionRewritingHook(object):
    """Import hook which rewrites asserts."""

    def __init__(self):
        self.session = None
        self.modules = {}

    def set_session(self, session):
        self.fnpats = session.config.getini("python_files")
        self.session = session

    def find_module(self, name, path=None):
        if self.session is None:
            return None
        sess = self.session
        state = sess.config._assertstate
        names = name.rsplit(".", 1)
        lastname = names[-1]
        pth = None
        if path is not None and len(path) == 1:
            pth = path[0]
        if pth is None:
            try:
                fd, fn, desc = imp.find_module(lastname, path)
            except ImportError:
                return None
            if fd is not None:
                fd.close()
            tp = desc[2]
            if tp == imp.PY_COMPILED:
                if hasattr(imp, "source_from_cache"):
                    fn = imp.source_from_cache(fn)
                else:
                    fn = fn[:-1]
            elif tp != imp.PY_SOURCE:
                # Don't know what this is.
                return None
        else:
            fn = os.path.join(pth, name + ".py")
        fn_pypath = py.path.local(fn)
        # Is this a test file?
        if not sess.isinitpath(fn):
            # We have to be very careful here because imports in this code can
            # trigger a cycle.
            self.session = None
            try:
                for pat in self.fnpats:
                    if fn_pypath.fnmatch(pat):
                        break
                else:
                    return None
            finally:
                self.session = sess
        # The requested module looks like a test file, so rewrite it. This is
        # the most magical part of the process: load the source, rewrite the
        # asserts, and load the rewritten source. We also cache the rewritten
        # module code in a special pyc. We must be aware of the possibility of
        # concurrent py.test processes rewriting and loading pycs. To avoid
        # tricky race conditions, we maintain the following invariant: The
        # cached pyc is always a complete, valid pyc. Operations on it must be
        # atomic. POSIX's atomic rename comes in handy.
        cache_dir = os.path.join(fn_pypath.dirname, "__pycache__")
        py.path.local(cache_dir).ensure(dir=True)
        cache_name = fn_pypath.basename[:-3] + "." + PYTEST_TAG + ".pyc"
        pyc = os.path.join(cache_dir, cache_name)
        co = _read_pyc(fn_pypath, pyc)
        if co is None:
            state.trace("rewriting %r" % (fn,))
            co = _make_rewritten_pyc(state, fn_pypath, pyc)
            if co is None:
                # Probably a SyntaxError in the module.
                return None
        else:
            state.trace("found cached rewritten pyc for %r" % (fn,))
        self.modules[name] = co, pyc
        return self

    def load_module(self, name):
        co, pyc = self.modules.pop(name)
        # I wish I could just call imp.load_compiled here, but __file__ has to
        # be set properly. In Python 3.2+, this all would be handled correctly
        # by load_compiled.
        mod = sys.modules[name] = imp.new_module(name)
        try:
            mod.__file__ = co.co_filename
            # Normally, this attribute is 3.2+.
            mod.__cached__ = pyc
            py.builtin.exec_(co, mod.__dict__)
        except:
            del sys.modules[name]
            raise
        return sys.modules[name]

def _write_pyc(co, source_path, pyc):
    # Technically, we don't have to have the same pyc format as (C)Python, since
    # these "pycs" should never be seen by builtin import. However, there's
    # little reason deviate, and I hope sometime to be able to use
    # imp.load_compiled to load them. (See the comment in load_module above.)
    mtime = int(source_path.mtime())
    fp = open(pyc, "wb")
    try:
        fp.write(imp.get_magic())
        fp.write(struct.pack("<l", mtime))
        marshal.dump(co, fp)
    finally:
        fp.close()

def _make_rewritten_pyc(state, fn, pyc):
    """Try to rewrite *fn* and dump the rewritten code to *pyc*.

    Return the code object of the rewritten module on success. Return None if
    there are problems parsing or compiling the module.
    """
    try:
        source = fn.read("rb")
    except EnvironmentError:
        return None
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # Let this pop up again in the real import.
        state.trace("failed to parse: %r" % (fn,))
        return None
    rewrite_asserts(tree)
    try:
        co = compile(tree, fn.strpath, "exec")
    except SyntaxError:
        # It's possible that this error is from some bug in the
        # assertion rewriting, but I don't know of a fast way to tell.
        state.trace("failed to compile: %r" % (fn,))
        return None
    if sys.platform.startswith("win"):
        # Windows grants exclusive access to open files and doesn't have atomic
        # rename, so just write into the final file.
        _write_pyc(co, fn, pyc)
    else:
        # When not on windows, assume rename is atomic. Dump the code object
        # into a file specific to this process and atomically replace it.
        proc_pyc = pyc + "." + str(os.getpid())
        _write_pyc(co, fn, proc_pyc)
        os.rename(proc_pyc, pyc)
    return co

def _read_pyc(source, pyc):
    """Possibly read a py.test pyc containing rewritten code.

    Return rewritten code if successful or None if not.
    """
    try:
        fp = open(pyc, "rb")
    except IOError:
        return None
    try:
        try:
            mtime = int(source.mtime())
            data = fp.read(8)
        except EnvironmentError:
            return None
        # Check for invalid or out of date pyc file.
        if (len(data) != 8 or
            data[:4] != imp.get_magic() or
            struct.unpack("<l", data[4:])[0] != mtime):
            return None
        co = marshal.load(fp)
        if not isinstance(co, types.CodeType):
            # That's interesting....
            return None
        return co
    finally:
        fp.close()


def rewrite_asserts(mod):
    """Rewrite the assert statements in mod."""
    AssertionRewriter().run(mod)


_saferepr = py.io.saferepr
from _pytest.assertion.util import format_explanation as _format_explanation

def _format_boolop(explanations, is_or):
    return "(" + (is_or and " or " or " and ").join(explanations) + ")"

def _call_reprcompare(ops, results, expls, each_obj):
    for i, res, expl in zip(range(len(ops)), results, expls):
        try:
            done = not res
        except Exception:
            done = True
        if done:
            break
    if util._reprcompare is not None:
        custom = util._reprcompare(ops[i], each_obj[i], each_obj[i + 1])
        if custom is not None:
            return custom
    return expl


unary_map = {
    ast.Not : "not %s",
    ast.Invert : "~%s",
    ast.USub : "-%s",
    ast.UAdd : "+%s"
}

binop_map = {
    ast.BitOr : "|",
    ast.BitXor : "^",
    ast.BitAnd : "&",
    ast.LShift : "<<",
    ast.RShift : ">>",
    ast.Add : "+",
    ast.Sub : "-",
    ast.Mult : "*",
    ast.Div : "/",
    ast.FloorDiv : "//",
    ast.Mod : "%",
    ast.Eq : "==",
    ast.NotEq : "!=",
    ast.Lt : "<",
    ast.LtE : "<=",
    ast.Gt : ">",
    ast.GtE : ">=",
    ast.Pow : "**",
    ast.Is : "is",
    ast.IsNot : "is not",
    ast.In : "in",
    ast.NotIn : "not in"
}


def set_location(node, lineno, col_offset):
    """Set node location information recursively."""
    def _fix(node, lineno, col_offset):
        if "lineno" in node._attributes:
            node.lineno = lineno
        if "col_offset" in node._attributes:
            node.col_offset = col_offset
        for child in ast.iter_child_nodes(node):
            _fix(child, lineno, col_offset)
    _fix(node, lineno, col_offset)
    return node


class AssertionRewriter(ast.NodeVisitor):

    def run(self, mod):
        """Find all assert statements in *mod* and rewrite them."""
        if not mod.body:
            # Nothing to do.
            return
        # Insert some special imports at the top of the module but after any
        # docstrings and __future__ imports.
        aliases = [ast.alias(py.builtin.builtins.__name__, "@py_builtins"),
                   ast.alias("_pytest.assertion.rewrite", "@pytest_ar")]
        expect_docstring = True
        pos = 0
        lineno = 0
        for item in mod.body:
            if (expect_docstring and isinstance(item, ast.Expr) and
                isinstance(item.value, ast.Str)):
                doc = item.value.s
                if "PYTEST_DONT_REWRITE" in doc:
                    # The module has disabled assertion rewriting.
                    return
                lineno += len(doc) - 1
                expect_docstring = False
            elif (not isinstance(item, ast.ImportFrom) or item.level > 0 or
                  item.module != "__future__"):
                lineno = item.lineno
                break
            pos += 1
        imports = [ast.Import([alias], lineno=lineno, col_offset=0)
                   for alias in aliases]
        mod.body[pos:pos] = imports
        # Collect asserts.
        nodes = [mod]
        while nodes:
            node = nodes.pop()
            for name, field in ast.iter_fields(node):
                if isinstance(field, list):
                    new = []
                    for i, child in enumerate(field):
                        if isinstance(child, ast.Assert):
                            # Transform assert.
                            new.extend(self.visit(child))
                        else:
                            new.append(child)
                            if isinstance(child, ast.AST):
                                nodes.append(child)
                    setattr(node, name, new)
                elif (isinstance(field, ast.AST) and
                      # Don't recurse into expressions as they can't contain
                      # asserts.
                      not isinstance(field, ast.expr)):
                    nodes.append(field)

    def variable(self):
        """Get a new variable."""
        # Use a character invalid in python identifiers to avoid clashing.
        name = "@py_assert" + str(next(self.variable_counter))
        self.variables[self.cond_chain].add(name)
        return name

    def assign(self, expr):
        """Give *expr* a name."""
        name = self.variable()
        self.statements.append(ast.Assign([ast.Name(name, ast.Store())], expr))
        return ast.Name(name, ast.Load())

    def display(self, expr):
        """Call py.io.saferepr on the expression."""
        return self.helper("saferepr", expr)

    def helper(self, name, *args):
        """Call a helper in this module."""
        py_name = ast.Name("@pytest_ar", ast.Load())
        attr = ast.Attribute(py_name, "_" + name, ast.Load())
        return ast.Call(attr, list(args), [], None, None)

    def builtin(self, name):
        """Return the builtin called *name*."""
        builtin_name = ast.Name("@py_builtins", ast.Load())
        return ast.Attribute(builtin_name, name, ast.Load())

    def explanation_param(self, expr):
        specifier = "py" + str(next(self.variable_counter))
        self.explanation_specifiers[specifier] = expr
        return "%(" + specifier + ")s"

    def enter_cond(self, cond, body):
        self.statements.append(ast.If(cond, body, []))
        self.cond_chain += cond,

    def leave_cond(self, n=1):
        self.cond_chain = self.cond_chain[:-n]

    def push_format_context(self):
        self.explanation_specifiers = {}
        self.stack.append(self.explanation_specifiers)

    def pop_format_context(self, expl_expr):
        current = self.stack.pop()
        if self.stack:
            self.explanation_specifiers = self.stack[-1]
        keys = [ast.Str(key) for key in current.keys()]
        format_dict = ast.Dict(keys, list(current.values()))
        form = ast.BinOp(expl_expr, ast.Mod(), format_dict)
        name = "@py_format" + str(next(self.variable_counter))
        self.on_failure.append(ast.Assign([ast.Name(name, ast.Store())], form))
        return ast.Name(name, ast.Load())

    def generic_visit(self, node):
        """Handle expressions we don't have custom code for."""
        assert isinstance(node, ast.expr)
        res = self.assign(node)
        return res, self.explanation_param(self.display(res))

    def visit_Assert(self, assert_):
        if assert_.msg:
            # There's already a message. Don't mess with it.
            return [assert_]
        self.statements = []
        self.cond_chain = ()
        self.variables = collections.defaultdict(set)
        self.variable_counter = itertools.count()
        self.stack = []
        self.on_failure = []
        self.push_format_context()
        # Rewrite assert into a bunch of statements.
        top_condition, explanation = self.visit(assert_.test)
        # Create failure message.
        body = self.on_failure
        negation = ast.UnaryOp(ast.Not(), top_condition)
        self.statements.append(ast.If(negation, body, []))
        explanation = "assert " + explanation
        template = ast.Str(explanation)
        msg = self.pop_format_context(template)
        fmt = self.helper("format_explanation", msg)
        err_name = ast.Name("AssertionError", ast.Load())
        exc = ast.Call(err_name, [fmt], [], None, None)
        if sys.version_info[0] >= 3:
            raise_ = ast.Raise(exc, None)
        else:
            raise_ = ast.Raise(exc, None, None)
        body.append(raise_)
        # Delete temporary variables. This requires a bit cleverness about the
        # order, so we don't delete variables that are themselves conditions for
        # later variables.
        for chain in sorted(self.variables, key=len, reverse=True):
            if chain:
                where = []
                if len(chain) > 1:
                    cond = ast.Boolop(ast.And(), chain)
                else:
                    cond = chain[0]
                self.statements.append(ast.If(cond, where, []))
            else:
                where = self.statements
            v = self.variables[chain]
            names = [ast.Name(name, ast.Del()) for name in v]
            where.append(ast.Delete(names))
        # Fix line numbers.
        for stmt in self.statements:
            set_location(stmt, assert_.lineno, assert_.col_offset)
        return self.statements

    def visit_Name(self, name):
        # Check if the name is local or not.
        locs = ast.Call(self.builtin("locals"), [], [], None, None)
        globs = ast.Call(self.builtin("globals"), [], [], None, None)
        ops = [ast.In(), ast.IsNot()]
        test = ast.Compare(ast.Str(name.id), ops, [locs, globs])
        expr = ast.IfExp(test, self.display(name), ast.Str(name.id))
        return name, self.explanation_param(expr)

    def visit_BoolOp(self, boolop):
        res_var = self.variable()
        expl_list = self.assign(ast.List([], ast.Load()))
        app = ast.Attribute(expl_list, "append", ast.Load())
        is_or = isinstance(boolop.op, ast.Or)
        body = save = self.statements
        levels = len(boolop.values) - 1
        self.push_format_context()
        # Process each operand, short-circuting if needed.
        for i, v in enumerate(boolop.values):
            res, expl = self.visit(v)
            body.append(ast.Assign([ast.Name(res_var, ast.Store())], res))
            call = ast.Call(app, [ast.Str(expl)], [], None, None)
            body.append(ast.Expr(call))
            if i < levels:
                inner = []
                cond = res
                if is_or:
                    cond = ast.UnaryOp(ast.Not(), cond)
                self.enter_cond(cond, inner)
                self.statements = body = inner
        # Leave all conditions.
        self.leave_cond(levels)
        self.statements = save
        expl_template = self.helper("format_boolop", expl_list, ast.Num(is_or))
        expl = self.pop_format_context(expl_template)
        return ast.Name(res_var, ast.Load()), self.explanation_param(expl)

    def visit_UnaryOp(self, unary):
        pattern = unary_map[unary.op.__class__]
        operand_res, operand_expl = self.visit(unary.operand)
        res = self.assign(ast.UnaryOp(unary.op, operand_res))
        return res, pattern % (operand_expl,)

    def visit_BinOp(self, binop):
        symbol = binop_map[binop.op.__class__]
        left_expr, left_expl = self.visit(binop.left)
        right_expr, right_expl = self.visit(binop.right)
        explanation = "(%s %s %s)" % (left_expl, symbol, right_expl)
        res = self.assign(ast.BinOp(left_expr, binop.op, right_expr))
        return res, explanation

    def visit_Call(self, call):
        new_func, func_expl = self.visit(call.func)
        arg_expls = []
        new_args = []
        new_kwargs = []
        new_star = new_kwarg = None
        for arg in call.args:
            res, expl = self.visit(arg)
            new_args.append(res)
            arg_expls.append(expl)
        for keyword in call.keywords:
            res, expl = self.visit(keyword.value)
            new_kwargs.append(ast.keyword(keyword.arg, res))
            arg_expls.append(keyword.arg + "=" + expl)
        if call.starargs:
            new_star, expl = self.visit(call.starargs)
            arg_expls.append("*" + expl)
        if call.kwargs:
            new_kwarg, expl = self.visit(call.kwarg)
            arg_expls.append("**" + expl)
        expl = "%s(%s)" % (func_expl, ', '.join(arg_expls))
        new_call = ast.Call(new_func, new_args, new_kwargs, new_star, new_kwarg)
        res = self.assign(new_call)
        res_expl = self.explanation_param(self.display(res))
        outer_expl = "%s\n{%s = %s\n}" % (res_expl, res_expl, expl)
        return res, outer_expl

    def visit_Attribute(self, attr):
        if not isinstance(attr.ctx, ast.Load):
            return self.generic_visit(attr)
        value, value_expl = self.visit(attr.value)
        res = self.assign(ast.Attribute(value, attr.attr, ast.Load()))
        res_expl = self.explanation_param(self.display(res))
        pat = "%s\n{%s = %s.%s\n}"
        expl = pat % (res_expl, res_expl, value_expl, attr.attr)
        return res, expl

    def visit_Compare(self, comp):
        self.push_format_context()
        left_res, left_expl = self.visit(comp.left)
        res_variables = [self.variable() for i in range(len(comp.ops))]
        load_names = [ast.Name(v, ast.Load()) for v in res_variables]
        store_names = [ast.Name(v, ast.Store()) for v in res_variables]
        it = zip(range(len(comp.ops)), comp.ops, comp.comparators)
        expls = []
        syms = []
        results = [left_res]
        for i, op, next_operand in it:
            next_res, next_expl = self.visit(next_operand)
            results.append(next_res)
            sym = binop_map[op.__class__]
            syms.append(ast.Str(sym))
            expl = "%s %s %s" % (left_expl, sym, next_expl)
            expls.append(ast.Str(expl))
            res_expr = ast.Compare(left_res, [op], [next_res])
            self.statements.append(ast.Assign([store_names[i]], res_expr))
            left_res, left_expl = next_res, next_expl
        # Use py.code._reprcompare if that's available.
        expl_call = self.helper("call_reprcompare",
                                ast.Tuple(syms, ast.Load()),
                                ast.Tuple(load_names, ast.Load()),
                                ast.Tuple(expls, ast.Load()),
                                ast.Tuple(results, ast.Load()))
        if len(comp.ops) > 1:
            res = ast.BoolOp(ast.And(), load_names)
        else:
            res = load_names[0]
        return res, self.explanation_param(self.pop_format_context(expl_call))