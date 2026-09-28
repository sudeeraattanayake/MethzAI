from langchain_tavily import TavilySearch
from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig
from dotenv import load_dotenv
from openai import OpenAI, APIStatusError, APIConnectionError
from pathlib import Path
from uuid import uuid4
from database import save_memory, search_memory
from rag import retrieve_from_rag
import ast
import base64
import logging
import math
import operator
import os

load_dotenv()

logger = logging.getLogger(__name__)

IMAGE_MODEL = os.getenv(
    "IMAGE_MODEL", "gpt-image-2.5-sunburst"
).strip()

IMAGE_DIR = (
    Path(__file__).resolve().parent / "data" / "generated_images"
)


def get_thread_id(config: RunnableConfig) -> str:
    thread_id = config.get("configurable", {}).get("thread_id")

    if not isinstance(thread_id, str) or not thread_id.strip():
        raise ValueError(
            "Missing thread_id. Pass it in config['configurable']."
        )

    return thread_id


web_search = TavilySearch(
    max_results=5,
    topic="general",
    search_depth="advanced"
)

BINARY_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow
}

BASIC_FUNCTIONS = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sum": sum
}

MATH_FUNCTIONS = {
    name: getattr(math, name)
    for name in (
        "sqrt", "sin", "cos", "tan",
        "log", "log10", "log2", "exp",
        "floor", "ceil", "fabs",
        "degrees", "radians"
    )
}

MATH_CONSTANTS = {
    "pi": math.pi,
    "e": math.e,
    "tau": math.tau
}


def check_number(value):
    if type(value) not in (int, float):
        raise ValueError("Only real numbers are supported.")

    if abs(value) > 10 ** 100:
        raise ValueError("Number is too large.")

    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Number must be finite.")

    return value


def calculate(expression: str):
    if not expression.strip() or len(expression) > 500:
        raise ValueError(
            "Use a math expression between 1 and 500 characters."
        )

    tree = ast.parse(expression, mode="eval")

    if sum(1 for _ in ast.walk(tree)) > 100:
        raise ValueError("Expression is too complex.")

    def visit(node):
        if isinstance(node, ast.Constant):
            return check_number(node.value)

        if isinstance(node, (ast.List, ast.Tuple)):
            return [
                check_number(visit(item))
                for item in node.elts
            ]

        if isinstance(node, ast.UnaryOp):
            if isinstance(node.op, (ast.UAdd, ast.USub)):
                value = check_number(visit(node.operand))
                return check_number(
                    -value if isinstance(node.op, ast.USub) else value
                )

        if isinstance(node, ast.BinOp):
            operation = BINARY_OPERATORS.get(type(node.op))

            if operation is not None:
                left = check_number(visit(node.left))
                right = check_number(visit(node.right))

                if isinstance(node.op, ast.Pow) and abs(right) > 100:
                    raise ValueError(
                        "Exponent must be between -100 and 100."
                    )

                return check_number(operation(left, right))

        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name):
                if (
                    node.value.id == "math"
                    and node.attr in MATH_CONSTANTS
                ):
                    return MATH_CONSTANTS[node.attr]

        if isinstance(node, ast.Call) and not node.keywords:
            function = None

            if isinstance(node.func, ast.Name):
                function = BASIC_FUNCTIONS.get(node.func.id)

            elif isinstance(node.func, ast.Attribute):
                if isinstance(node.func.value, ast.Name):
                    if node.func.value.id == "math":
                        function = MATH_FUNCTIONS.get(node.func.attr)

            if function is not None:
                arguments = [visit(arg) for arg in node.args]
                return check_number(function(*arguments))

        raise ValueError("Unsupported expression or function.")

    return check_number(visit(tree.body))


@tool
def calculator(expression: str) -> str:
    """
    Calculate a restricted arithmetic expression.
    Supports +, -, *, /, //, %, **, abs, round, min, max, sum,
    and selected math functions and constants.
    Examples: 2 + 2, math.sqrt(16), sum([1, 2, 3]), round(math.pi, 2).
    Use ** for powers. Trigonometric functions use radians.
    """
    try:
        return str(calculate(expression.strip()))
    except (
        ValueError,
        TypeError,
        SyntaxError,
        ArithmeticError,
        RecursionError
    ) as error:
        return f"Calculation error: {error}"


@tool
def search_uploaded_documents(
    query: str,
    config: RunnableConfig
) -> str:
    """
    Search documents uploaded to the current conversation.
    Use for questions about uploaded PDFs, DOCX, TXT, notes, or files.
    """
    query = query.strip()

    if not query:
        return "Please provide a document search query."

    return retrieve_from_rag(
        query=query,
        thread_id=get_thread_id(config)
    )


@tool
def remember_this(
    memory: str,
    config: RunnableConfig
) -> str:
    """
    Save a user preference or fact for the current conversation.
    Use when the user explicitly asks you to remember something.
    """
    memory = memory.strip()

    if not memory:
        return "Memory was not saved because it was empty."

    return save_memory(
        thread_id=get_thread_id(config),
        memory=memory
    )


@tool
def recall_memory(
    query: str,
    config: RunnableConfig
) -> str:
    """
    Recall saved memories from the current conversation.
    Use a short search phrase, or an empty query for recent memories.
    """
    return search_memory(
        thread_id=get_thread_id(config),
        query=query.strip()
    )


@tool
def generate_image(prompt: str) -> dict:
    """
    Generate a new image from a detailed text description.
    Use for requests to create an image, illustration, or artwork.
    Returns the saved PNG's local path and filename on success.
    Does not edit uploaded images.
    """
    prompt = prompt.strip()

    if not prompt:
        return {
            "status": "error",
            "message": "Please provide an image description."
        }

    if not os.getenv("OPENAI_API_KEY", "").strip():
        return {
            "status": "error",
            "message": "OPENAI_API_KEY is not configured."
        }

    if not IMAGE_MODEL:
        return {
            "status": "error",
            "message": "IMAGE_MODEL is empty. Check your .env file."
        }

    try:
        IMAGE_DIR.mkdir(parents=True, exist_ok=True)

        with OpenAI(timeout=180.0, max_retries=0) as client:
            result = client.images.generate(
                model=IMAGE_MODEL,
                prompt=prompt,
                size="1024x1024",
                quality="medium",
                output_format="png",
                n=1
            )

        if not result.data or not result.data[0].b64_json:
            return {
                "status": "error",
                "message": "No image was returned."
            }

        image_bytes = base64.b64decode(
            result.data[0].b64_json,
            validate=True
        )

        if not image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            return {
                "status": "error",
                "message": "The image service did not return a valid PNG."
            }

        filename = f"{uuid4().hex}.png"
        image_path = IMAGE_DIR / filename

        try:
            image_path.write_bytes(image_bytes)
        except OSError:
            image_path.unlink(missing_ok=True)
            raise

        return {
            "status": "success",
            "message": "Image generated successfully.",
            "image_path": str(image_path),
            "filename": filename,
            "mime_type": "image/png"
        }

    except APIStatusError as error:
        logger.exception("Image generation API request failed")

        if error.status_code in (401, 403):
            message = (
                "Image generation access was denied. "
                "Check your API key and model permissions."
            )
        elif error.status_code == 429:
            message = (
                "Image generation reached an API rate or usage limit. "
                "Check your quota and billing."
            )
        elif error.status_code == 400:
            message = (
                "The image request was rejected. "
                "Check the prompt and model settings in the server logs."
            )
        elif error.status_code == 404:
            message = (
                "The configured image model was not found "
                "or is unavailable to this API account."
            )
        else:
            message = (
                "The image service returned an error. "
                "Check the server logs."
            )

        return {"status": "error", "message": message}

    except APIConnectionError:
        logger.exception("Could not connect to image generation API")
        return {
            "status": "error",
            "message": (
                "Could not reach the image service or the request timed out. "
                "Check your connection and server logs."
            )
        }

    except Exception:
        logger.exception("Image generation or file saving failed")
        return {
            "status": "error",
            "message": (
                "Image generation or saving failed. "
                "Check the server logs for details."
            )
        }


tools = [
    calculator,
    search_uploaded_documents,
    remember_this,
    recall_memory,
    web_search,
    generate_image
]
