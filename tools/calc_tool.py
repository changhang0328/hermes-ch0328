#!/usr/bin/env python3
"""
Calc Tool - 基于 Python decimal 模块的精确数学计算工具。

金融场景对计算精度要求极高，浮点数存在舍入误差（如 0.1 + 0.2 != 0.3），
因此本工具使用 Decimal + 安全 AST 求值，避免 eval 风险。

支持：
  - 四则运算、幂运算、括号
  - 内置函数：sqrt, log, log10, log2, exp, abs, round, min, max, pow
  - 常量：PI, E
  - 通过 precision 参数控制有效位数（默认 28 位）
"""

import ast
import logging
import math
from decimal import Decimal, getcontext, InvalidOperation, DivisionByZero

from tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

# 安全的内置函数白名单
_SAFE_FUNCTIONS = {
    "sqrt": lambda x: x.sqrt(),
    "log": lambda x: x.ln(),
    "log10": lambda x: x.log10(),
    "log2": lambda x: x.ln() / Decimal(2).ln(),
    "exp": lambda x: (x.exp()),
    "abs": lambda x: abs(x),
    "round": lambda x, n=0: round(x, int(n)),
    "min": min,
    "max": max,
    "pow": lambda x, y: x ** y,
}

_SAFE_CONSTANTS = {
    "PI": Decimal(str(math.pi)),
    "E": Decimal(str(math.e)),
}


class _CalcVisitor(ast.NodeVisitor):
    """安全 AST 求值器 —— 只允许数值、运算符和白名单函数。"""

    def __init__(self, precision: int = 28):
        getcontext().prec = precision
        self._precision = precision

    def evaluate(self, expr: str) -> Decimal:
        tree = ast.parse(expr, mode="eval")
        return self.visit(tree.body)

    def visit_Expression(self, node):
        return self.visit(node.body)

    def visit_BinOp(self, node):
        left = self.visit(node.left)
        right = self.visit(node.right)
        ops = {
            ast.Add: lambda a, b: a + b,
            ast.Sub: lambda a, b: a - b,
            ast.Mult: lambda a, b: a * b,
            ast.Div: lambda a, b: a / b,
            ast.FloorDiv: lambda a, b: a // b,
            ast.Mod: lambda a, b: a % b,
            ast.Pow: lambda a, b: a ** b,
        }
        op_fn = ops.get(type(node.op))
        if op_fn is None:
            raise ValueError(f"Unsupported operator: {type(node.op).__name__}")
        try:
            return op_fn(left, right)
        except DivisionByZero:
            raise ValueError("Division by zero")
        except InvalidOperation as exc:
            raise ValueError(f"Invalid operation: {exc}")

    def visit_UnaryOp(self, node):
        operand = self.visit(node.operand)
        if isinstance(node.op, ast.UAdd):
            return +operand
        if isinstance(node.op, ast.USub):
            return -operand
        raise ValueError(f"Unsupported unary operator: {type(node.op).__name__}")

    def visit_Call(self, node):
        if not isinstance(node.func, ast.Name):
            raise ValueError(f"Unsupported call: only plain function names allowed")
        fn_name = node.func.id
        if fn_name not in _SAFE_FUNCTIONS:
            raise ValueError(f"Function not allowed: {fn_name}")
        args = [self.visit(arg) for arg in node.args]
        try:
            return _SAFE_FUNCTIONS[fn_name](*args)
        except (InvalidOperation, DivisionByZero, ValueError) as exc:
            raise ValueError(f"Error in {fn_name}(): {exc}")

    def visit_Name(self, node):
        if node.id in _SAFE_CONSTANTS:
            return _SAFE_CONSTANTS[node.id]
        raise ValueError(f"Name not allowed: {node.id}")

    def visit_Constant(self, node):
        if isinstance(node.value, (int, float)):
            return Decimal(str(node.value))
        raise ValueError(f"Unsupported constant type: {type(node.value).__name__}")

    def visit_Num(self, node):  # Python 3.7 compat
        return Decimal(str(node.n))

    def generic_visit(self, node):
        raise ValueError(f"Unsupported syntax: {type(node).__name__}")


def calc_eval(expression: str, precision: int = 28) -> str:
    """
    精确计算数学表达式。

    Args:
        expression: 数学表达式字符串，如 "0.1 + 0.2", "sqrt(2) * PI", "100 * (1 + 0.05) ** 10"
        precision: Decimal 有效位数，默认 28

    Returns:
        JSON 字符串，含 result 字段（精确数值）和 expression 字段。
    """
    if not isinstance(expression, str):
        return tool_error("expression must be a string")
    expression = expression.strip()
    if not expression:
        return tool_error("expression is required and cannot be empty")

    precision = int(precision)
    if precision < 1 or precision > 100:
        return tool_error("precision must be between 1 and 100")

    try:
        evaluator = _CalcVisitor(precision=precision)
        result = evaluator.evaluate(expression)
    except SyntaxError as exc:
        return tool_error(f"Syntax error in expression: {exc}", kind="syntax_error")
    except ValueError as exc:
        return tool_error(str(exc), kind="value_error")
    except DivisionByZero:
        return tool_error("Division by zero", kind="division_by_zero")
    except InvalidOperation as exc:
        return tool_error(f"Invalid operation: {exc}", kind="invalid_operation")
    except OverflowError:
        return tool_error("Result too large (overflow)", kind="overflow")
    except Exception as exc:
        logger.exception("[calc_tool] unexpected error")
        return tool_error(f"Calculation error: {exc}", kind="unknown")

    # 格式化：去掉 Decimal 尾部无意义零，保持精度
    result_str = format(result, "f")

    return tool_result(
        expression=expression,
        result=result_str,
        precision=precision,
    )


def check_calc_requirements() -> bool:
    """纯 Python 标准库实现，无外部依赖，始终可用。"""
    return True


CALC_SCHEMA = {
    "name": "calc_eval",
    "description": (
        "精确数学计算工具，基于 Python decimal 模块，避免浮点数舍入误差。"
        "支持四则运算、幂运算、括号，以及 sqrt/log/log10/log2/exp/abs/round/min/max/pow "
        "函数和 PI/E 常量。适用于金融场景中需要高精度的计算，如收益率、复利、"
        "加权平均等。不要用它做符号推导，它只做数值求值。"
        "\n\n用法示例："
        "\n  calc_eval(expression=\"0.1 + 0.2\")             → 0.3"
        "\n  calc_eval(expression=\"100 * (1 + 0.05) ** 10\") → 复利计算"
        "\n  calc_eval(expression=\"sqrt(2) * PI\")          → 常量运算"
        "\n  calc_eval(expression=\"round(3.1415, 2)\")      → 3.14"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "要计算的数学表达式（必填，非空）。",
            },
            "precision": {
                "type": "integer",
                "description": "Decimal 有效位数，默认 28。金融场景建议 28 即可。",
            },
        },
        "required": ["expression"],
    },
}


registry.register(
    name="calc_eval",
    toolset="calc",
    schema=CALC_SCHEMA,
    handler=lambda args, **kw: calc_eval(
        expression=args.get("expression", ""),
        precision=args.get("precision", 28),
    ),
    check_fn=check_calc_requirements,
    emoji="🧮",
)