import re
from collections import defaultdict
from itertools import chain, count
from string import capwords

try:
    from . import Errors
    from .AST import *
    from .Workshop import Workshop
except ImportError:
    from AST import *

def flatten(l):
    l = list(l)
    while l:
        while l and isinstance(l[0], list):
            l[0:1] = l[0]
        if l: yield l.pop(0)

class Scope:
    def __init__(self, name, parent=None, namespace=None, level=0):
        self.name = name
        self.parent = parent
        self.namespace = namespace or {}
        self.level = 0

    def get(self, name, default=None):
        value = self.namespace.get(name, default)
        if value is not None:
            return value
        if self.parent:
            return self.parent.get(name, default)

    def assign(self, name, value, index=None):
        self.namespace[name] = Variable(value=value, index=index)

    def __repr__(self):
        return f"<Scope '{self.name}'[{self.level}]>"

class Builtin:
    def range(*args):
        args = list(map(int, args))
        elements = list(map(Number, map(str, range(*args))))
        array = Array(elements=elements)
        return array

    def ceil(tp, n):
        node = OWID(name='Round To Integer')
        node.children.extend([*n, OWID(name='Up')])
        return node

    def floor(tp, n):
        node = OWID(name='Round To Integer')
        node.children.extend([*n, OWID(name='Up')])
        return node

    range: range
    ceil: ceil
    floor: floor

class Transpiler:
    def __init__(self, tree, indent_size=3):
        self.tree = tree
        self.indent_size = indent_size
        self.indent_level = 0
        self.global_index = count()
        self.player_index = count()
        self.curblock = []

    @property
    def tabs(self):
        return ' ' * self.indent_size * self.indent_level

    @property
    def min_wait(self):
        return 'Wait(0.016, Ignore Condition)'

    def base_node(self, node):
        while type(node) in (Attribute, Call, Item):
            node = node.parent
        return node

    def resolve_skips(self):
        # Resolves for/while continue skips
        cur_line = 0
        skips = []
        skip_to = []
        for line_no, line in enumerate(self.curblock):
            match = re.match(r'\s*//SKIP TO', line)
            if match:
                skips.append(line_no)
                self.curblock[line_no] = re.sub('//SKIP TO', '', line)
            match = re.match(r'\s*//FOR START', line)
            if match:
                skip_to.append(line_no - 1)
                self.curblock[line_no] = re.sub('//FOR START', '', line)
        for skip, jump in zip(skips, skip_to[::-1]):
            self.curblock[skip] = self.curblock[skip].format(jump)

    def visitScript(self, node, scope):
        code = r'rule("Generated by https://github.com/adapap/OWScript") { Event { Ongoing - Global; }}' + '\n'
        return (code + ''.join(self.visit_children(node, scope))).rstrip('\n')

    def visitRule(self, node, scope):
        code = ''
        if node.disabled:
            code += 'disabled '
        code += 'rule('
        code += '"' + ''.join(x if type(x) == str else self.visit(x, scope) for x in node.name) + '"'
        code += ') {\n' + '\n'.join(self.visit_children(node, scope)) + '}\n'
        return code

    def visitFunction(self, node, scope):
        scope.assign('gvar_' + node.name, node)
        return ''

    def visitBlock(self, node, scope):
        self.indent_level += 1
        code = ''.join(self.visit_children(node, scope))
        self.indent_level -= 1
        return code

    def visitRuleblock(self, node, scope):
        code = self.tabs + node.name + ' {\n'
        self.indent_level += 1
        for ruleblock in node.children:
            self.curblock = []
            for line in ruleblock.children:
                result = self.visit(line, scope)
                if result:
                    result = result.rstrip(';\n').split(';\n')
                    for x in result:
                        child = self.tabs + x
                        if node.name.upper() == 'CONDITIONS':
                            child += ' == True'
                        self.curblock.append(child)
            self.resolve_skips()
            code += ';\n'.join(self.curblock)
        self.indent_level -= 1
        code += ';\n' + self.tabs + '}\n'
        return code

    def visitOWID(self, node, scope):
        name = node.name.title()
        code = name
        try:
            assert len(node.args) == len(node.children)
        except AssertionError:
            raise Errors.SyntaxError('\'{}\' expected {} arguments ({}), received {}'.format(
                name, len(node.args), ', '.join(map(lambda arg: arg.__name__, node.args)), len(node.children))
            , pos=node._pos)
        for index, types in enumerate(zip(node.args, node.children)):
            arg, child = types
            if arg is None:
                continue
            extends = arg._extends if hasattr(arg, '_extends') else []
            values = list(flatten(arg.get_values()))
            if 'ANY' in values:
                continue
            value = self.visit(child, scope).upper()
            if value not in values:
                raise Errors.InvalidParameter('\'{}\' expected type {} for argument {}, received {}'.format(
                    name, arg.__name__, index + 1, child.__class__.__name__)
                , pos=child._pos)
        children = [self.visit(child, scope) for child in node.children]
        code += '(' + ', '.join(children) + ')'
        return code

    def visitConstant(self, node, scope):
        return node.name.title()

    def visitCompare(self, node, scope):
        if node.op.lower() == 'in':
            return 'Array Contains(' + self.visit(node.right, scope) + ', ' + self.visit(node.left, scope) + ')'
        elif node.op.lower() == 'not in':
            return 'Not(Array Contains(' + self.visit(node.right, scope) + ', ' + self.visit(node.left, scope) + '))'
        return 'Compare(' + self.visit(node.left, scope) + f', {node.op}, ' + self.visit(node.right, scope) + ')'

    def visitAssign(self, node, scope):
        code = ''
        value = {
            '+=': BinaryOp(left=node.left, op='+', right=node.right),
            '-=': BinaryOp(left=node.left, op='-', right=node.right),
            '*=': BinaryOp(left=node.left, op='*', right=node.right),
            '/=': BinaryOp(left=node.left, op='/', right=node.right),
            '^=': BinaryOp(left=node.left, op='^', right=node.right),
            '%=': BinaryOp(left=node.left, op='%', right=node.right)
        }.get(node.op, node.right)
        # Configure value
        if type(node.right) == OWID:
            pass
        # Define variables
        if type(node.left) == GlobalVar:
            name = node.left.name
            var = scope.get(name)
            index = var.index if var else next(self.global_index)
            scope.assign(name=name, value=value, index=index)
        elif type(node.left) == PlayerVar:
            name = node.left.name
            var = scope.get(name)
            index = var.index if var else next(self.player_index)
            player = node.left.player
            scope.assign(name=name, value=value, index=index)
        elif type(node.left) == Item:
            parent = node.left.parent
            name = parent.name
            var = scope.get(name)
            try:
                index = int(self.visit(node.left.index, scope))
                var.value[index] = value
                value = var.value
            except ValueError:
                # create temp variable, adjust and rebuild array
                # index = self.visit(node.left.index)
                raise Errors.NotImplementedError('Array assignment only supports literal indices', pos=node._pos)
            player = self.visit(parent.player, scope) if type(parent) == PlayerVar else None
            scope.assign(name=name, value=value, index=index)
        else:
            raise Errors.NotImplementedError('Assign to \'{}\' not implemented'.format(type(node.left).__name__), pos=node._pos)
        if name.startswith('gvar_'):
            code += 'Set Global Variable At Index(A, {}, {})'.format(index, self.visit(value, scope))
        elif name.startswith('pvar_'):
            code += 'Set Player Variable At Index({}, A, {}, {})'.format(self.visit(player, scope), index, self.visit(value, scope))
        return code

    def visitIf(self, node, scope):
        cond = self.visit(node.cond, scope)
        skip_code = 'Skip If(Not({}), {});\n'
        skip_false = ''
        true_code = ''.join(self.visit_children(node.true_block, scope))
        false_code = ''
        if node.false_block:
            skip_false = self.tabs + 'Skip({});\n'
            if type(node.false_block) == If:
                false_code += self.visit(node.false_block, scope)
            else:
                for line in node.false_block.children:
                    false_code += self.tabs + self.visit(line, scope) + ';\n'
        skip_code = skip_code.format(cond, true_code.count(';\n') + bool(node.false_block))
        if false_code:
            skip_false = skip_false.format(false_code.count(';\n') + bool(type(node.false_block) == If))
        code = skip_code + true_code + skip_false + false_code
        return code

    def visitWhile(self, node, scope):
        lines = len(node.body.children) + 2
        code = f'Skip If(Not({self.visit(node.cond, scope)}), {lines});\n'
        for line in node.body.children:
            code += self.tabs + self.visit(line, scope) + ';\n'
        code += f'{self.tabs}{self.min_wait};\n'
        code += f'{self.tabs}Loop If({self.visit(node.cond, scope)})'
        return code

    def visitFor(self, node, scope):
        code = ''
        pointer = node.pointer
        iterable = node.iterable
        if type(iterable) in (GlobalVar, PlayerVar) and type(scope.get(iterable.name).value) == Array:
            lines = []
            array = scope.get(iterable.name).value
            for elem in scope.get(iterable.name).value.elements:
                scope = Scope(name='for', parent=scope)
                scope.assign('{}'.format(pointer.name), value=elem)
                lines.append(';\n'.join(self.visit_children(node.body, scope)))
            code += ';\n'.join(lines)
        else:
            for_scope = Scope(name='for', parent=scope)
            value = Number(value='0')
            index = next(self.global_index)
            for_scope.assign('{}'.format(pointer.name), value=value, index=index)
            reset_pointer = 'Set Global Variable At Index(A, {}, 0);\n'.format(index)
            code += reset_pointer
            skip_code = '//FOR STARTSkip If(Compare(Count Of({}), ==, {}), {})'.format(self.visit(iterable, for_scope), self.visit(pointer, for_scope), '{}')
            block = ';\n'.join(self.visit_children(node.body, for_scope) + [
                'Modify Global Variable At Index(A, {}, Add, 1)'.format(index),
                self.min_wait,
                'Loop',
                reset_pointer])
            code += skip_code.format(block.count(';\n')) + ';\n' + block
            self.curblock.insert(0, self.tabs + '//SKIP TOSkip If(Compare(Value In Array(Global Variable(A), {}), !=, 0), {})'.format(index, '{}'))
        return code

    def visitBinaryOp(self, node, scope):
        if type(node.left) == Number and type(node.right) == Number:
            func = {
                '+': lambda a, b: a + b,
                '-': lambda a, b: a - b,
                '*': lambda a, b: a * b,
                '/': lambda a, b: a / b,
                '^': lambda a, b: a ** b,
                '%': lambda a, b: a % b
            }.get(node.op, None)
            if func:
                try:
                    result = func(node.left, node.right)
                    return self.visit(Number(value='{}'.format(result)), scope)
                except ZeroDivisionError:
                    return self.visit(Number(value='0'), scope)
        code = {
            '+': 'Add',
            '-': 'Subtract',
            '*': 'Multiply',
            '/': 'Divide',
            '^': 'Raise To Power',
            '%': 'Modulo',
            'or': 'Or',
            'and': 'And'
        }.get(node.op)
        code += '(' + self.visit(node.left, scope) + ', ' + self.visit(node.right, scope) + ')'
        return code

    def visitUnaryOp(self, node, scope):
        if node.op == '-':
            code = '-' + self.visit(node.right, scope)
        elif node.op == '+':
            code = 'Abs(' + self.visit(node.right, scope) + ')'
        elif node.op == 'not':
            code = 'Not(' + self.visit(node.right, scope) + ')'
        return code

    def visitGlobalVar(self, node, scope):
        name = node.name
        var = scope.get(name)
        if not var:
            raise Errors.NameError('\'{}\' is undefined'.format(node.name[5:]), pos=node._pos)
        elif type(var) != Variable:
            return self.visit(var, scope)
        elif type(var.value) == String:
            return var.value.value
        code = 'Value In Array(Global Variable(A), {})'.format(var.index)
        return code

    def visitPlayerVar(self, node, scope):
        name = node.name
        var = scope.get(name)
        if not var:
            raise Errors.NameError('pvar \'{}\' is undefined'.format(node.name[5:]), pos=node._pos)
        elif type(var) != Variable:
            return self.visit(var, scope)
        elif type(var.value) == String:
            return var.value.value
        code = 'Value In Array(Player Variable({}, A), {})'.format(self.visit(node.player, scope), var.index)
        return code

    def visitString(self, node, scope):
        code = 'String("' + node.value.title() + '", '
        children = ', '.join(self.visit(child, scope) for child in node.children)
        code += children + ')'
        return code

    def visitNumber(self, node, scope):
        return node.value

    def visitTime(self, node, scope):
        time = node.value
        if time.endswith('ms'):
            time = float(time.rstrip('ms')) / 1000
        elif time.endswith('s'):
            time = float(time.rstrip('s'))
        elif time.endswith('min'):
            time = float(time.rstrip('min')) * 60
        return str(round(time, 3))

    def visitVector(self, node, scope):
        code = 'Vector('
        components = ', '.join(self.visit(x, scope) for x in node.children)
        code += components + ')'
        return code

    def visitArray(self, node, scope):
        if not node.elements:
            return 'Empty Array'
        else:
            elements = []
            for elem in node.elements:
                if type(elem) in (String, Constant):
                    elements.append(Constant(name='Null'))
                else:
                    elements.append(elem)
            num_elems = len(elements)
            if num_elems == 0:
                return 'Empty Array'
            code = 'Append To Array(' * num_elems
            code += 'Empty Array, ' + '), '.join(self.visit(elem, scope) for elem in elements) + ')'
        return code

    def visitItem(self, node, scope):
        if type(node.index) == Number and type(node.parent) in (GlobalVar, PlayerVar):
            try:
                index = int(node.index.value)
                var = scope.get(node.parent.name)
                if not var:
                    raise Errors.NameError('{}\'{}\' is undefined'.format('pvar ' if type(node.parent) == PlayerVar else '', node.parent.name[5:]), pos=node.parent._pos)
                try:
                    item = var.value[index]
                except IndexError:
                    item = Number(value='0')
                return self.visit(item, scope)
            except ValueError:
                pass
        return 'Value In Array(' + self.visit(node.parent, scope) + ', ' + self.visit(node.index, scope) + ')'

    def visitAttribute(self, node, scope):
        attr = node.name.lower()
        try:
            attribute = getattr(node.parent, attr)
        except AttributeError:
            raise Errors.AttributeError('\'{}\' has no attribute \'{}\''.format(node.parent.name.title(), attr), pos=node._pos)
        code = attribute.format(self.visit(node.parent, scope))
        return code

    def visitCall(self, node, scope):
        parent = node.parent
        base_name = self.base_node(node).name
        base_node = scope.get(base_name)
        code = ''
        if type(parent) == Attribute:
            method = getattr(base_node.value, parent.name)
            try:
                result = method(*node.args)
            except TypeError:
                raise Errors.InvalidParameter("'{}' method received invalid arguments".format(parent.name), pos=parent._pos)
            if result:
                code += self.visit(result, scope)
            return code
        elif type(parent) in (GlobalVar, PlayerVar):
            func_name = parent.name[5:]
        else:
            print('DEBUG - called by:', type(parent))
        if not base_node:
            raise Errors.NameError('Undefined function \'{}\''.format(base_name[5:]), pos=parent._pos)
        func = base_node if type(base_node) != Variable else base_node.value
        if type(func) == Function:
            # Assert arity
            try:
                assert len(func.params) == len(node.args)
            except AssertionError:
                raise Errors.InvalidParameter('\'{}\' expected {} arguments, received {}'.format(func_name, len(func.params), len(node.args)), pos=node._pos)
            # Resolve variables in call
            scope = Scope(name=func_name, parent=scope)
            scope.namespace.update(dict(zip(map(lambda p: p.name, func.params), node.args)))
            for child in func.children:
                try:
                    code += self.visit(child, scope=scope)
                except Errors.ReturnError as ex:
                    code += self.visit(ex.value, scope=scope)
        else:
            try:
                result = func(*node.args)
                code += self.visit(result, scope)
            except TypeError as ex:
                print('typeerror', ex)
        return code

    def visitReturn(self, node, scope):
        if node.value is not None:
            raise Errors.ReturnError(value=node.value)
        return ''

    def visit(self, node, scope):
        method_name = 'visit' + type(node).__name__
        visitor = getattr(self, method_name)
        return visitor(node, scope)

    def visit_children(self, node, scope):
        lines = []
        for child in node.children:
            lines.append(self.visit(child, scope))
        return lines

    def run(self):
        global_scope = Scope(name='global')
        global_scope.namespace.update({'gvar_' + name: func for name, func in Builtin.__annotations__.items()})
        code = self.visit(self.tree, scope=global_scope)
        return code