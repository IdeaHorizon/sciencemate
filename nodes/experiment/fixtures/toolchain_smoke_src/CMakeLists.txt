cmake_minimum_required(VERSION 3.16)
project(hf_toolchain_smoke LANGUAGES C)
add_executable(hf_toolchain_smoke main.c)
install(TARGETS hf_toolchain_smoke RUNTIME DESTINATION bin)
