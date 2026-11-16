"""A result is consumable only after its separately durable publication marker."""


def complete(directory):
    return (directory / 'solver_result.json').is_file() and (directory / 'solver_publication.json').is_file()


def check_after_wait(directory, process, clock, stop):
    clock.check()
    code = process.poll()
    if code is not None and code != 0:
        raise stop('SOLVER_NONZERO_EXIT')
    if not complete(directory):
        raise stop('SOLVER_INCOMPLETE_DURABLE_PUBLICATION')
    # The same live solver must still be allowed to serve the parent-C request.
    # Its exit is deliberately not a completion precondition here.
    return True
