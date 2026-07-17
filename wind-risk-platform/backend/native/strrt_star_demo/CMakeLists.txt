cmake_minimum_required(VERSION 3.20)

project(wind_risk_strrt_star_native LANGUAGES CXX)

find_package(ompl CONFIG REQUIRED)

add_executable(strrt_star_native_demo strrt_star_native_demo.cpp)
target_compile_features(strrt_star_native_demo PRIVATE cxx_std_17)
target_link_libraries(strrt_star_native_demo PRIVATE ompl::ompl)

if(MSVC)
    target_compile_options(strrt_star_native_demo PRIVATE /utf-8 /W4)
endif()
