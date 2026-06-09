"""Competition submission entrypoint.

The evaluation server imports this file from the decompressed submission root.
Do not change the function name or signature.
"""


def algorithm(prob_info, timelimit=60):
    from ogc_solver.solver import solve

    return solve(prob_info, timelimit)
