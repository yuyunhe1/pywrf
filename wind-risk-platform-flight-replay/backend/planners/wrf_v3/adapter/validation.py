"""Platform endpoint policy around the unchanged independent V3 replay.

The user permits a one metre terminal position error. Heading, controls,
interior switch-node consistency, and all domain checks retain their upstream
limits. The receipt explicitly distinguishes this policy from upstream C.
"""
import numpy as np
from phase3b.validator import validate as upstream_validate
from .native import R0

ENDPOINT_TOLERANCE_M=1.


def validate_route(route, controls, duration, provider, *, max_step=4.):
    original=upstream_validate(route,controls,duration,provider,max_step=max_step)
    result={**original,"method":"INDEPENDENT_DOP853_PLATFORM_ENDPOINT_1M",
        "numerical_max_step_s":max_step,
        "upstream_valid":original["valid"],"upstream_failures":list(original["failures"]),
        "upstream_thresholds":dict(original["thresholds"]),
        "thresholds":{**original["thresholds"],"position_over_R0":ENDPOINT_TOLERANCE_M/R0,
                      "endpoint_position_m":ENDPOINT_TOLERANCE_M}}
    failures=list(original["failures"])
    if original.get("position_error") is not None and original["position_error"]<=ENDPOINT_TOLERANCE_M:
        failures=[f for f in failures if f!="POSITION_RESIDUAL"]
        if "INTERIOR_NODE_RESIDUAL" in failures and "states" in route:
            actual=np.asarray(original["diagnostic_nodes"],float)[:,1:]
            stored=np.asarray(route["states"],float)
            delta=(stored-actual)/[R0,R0,1.]
            interior=float(np.linalg.norm(delta[:-1],axis=1).max(initial=0.))
            final_position=float(np.linalg.norm(stored[-1,:2]-actual[-1,:2]))
            final_heading=abs(float(stored[-1,2]-actual[-1,2]))
            if interior<=original["thresholds"]["interior_node_scaled"] and final_position<=ENDPOINT_TOLERANCE_M and final_heading<=original["thresholds"]["interior_node_scaled"]:
                failures.remove("INTERIOR_NODE_RESIDUAL")
                result["constraint_residual"]={**original["constraint_residual"],
                    "interior_node_scaled":interior,"terminal_node_position_m":final_position}
    result["valid"]=not failures
    result["failures"]=failures
    result["validation_status"]="PASSED_PLATFORM_ENDPOINT_1M" if result["valid"] else "FAILED"
    if result["valid"]:
        result["time"]=original["diagnostic_elapsed_s"]
        result["trajectory"]={"switch_nodes_t_x_y_psi":original["diagnostic_nodes"],"z_layer_msl_m":route["z_layer_msl_m"]}
    return result
