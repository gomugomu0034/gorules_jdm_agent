import json
import zen

def check_jdm_format(jdm_content: str, tests_content: str) -> tuple[bool, any]:
    """
    Validates the syntax and structure of the JDM graph and Test Suite.
    Returns:
    (True, (parsed_jdm, parsed_tests)) if successful.
    (False, error_message) if validation fails.
    """
    try:
        parsed_jdm = json.loads(jdm_content)
        parsed_tests = json.loads(tests_content)

        if "nodes" not in parsed_jdm or "edges" not in parsed_jdm:
            return False, "JDM missing 'nodes' or 'edges'."

        if not isinstance(parsed_tests, list):
            return False, "Test suite must be a JSON array."

        return True, (parsed_jdm, parsed_tests)

    except Exception as e:
        return False, str(e)


def evaluate_against_zen(jdm_content: str, parsed_tests: list) -> tuple[bool, str]:
    """
    Compiles the JDM graph and executes it against the test suite.
    """
    try:
        engine = zen.ZenEngine()
        decision = engine.create_decision(jdm_content)

        evaluation_results = []
        for i, test_case in enumerate(parsed_tests):
            payload = test_case

            # If the LLM wrapped the payload inside an "input" key, extract it!
            if isinstance(test_case, dict) and "input" in test_case:
                payload = test_case["input"]

            # Evaluate using the clean payload
            result = decision.evaluate(payload)

            # Serialize the result back to string for clean feedback
            evaluation_results.append(f"Test {i + 1} Output: {json.dumps(result)}")

        return True, "\n".join(evaluation_results)

    except Exception as e:
        return False, str(e)