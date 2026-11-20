cmake_minimum_required(VERSION 3.22.1)
project("dsrc_dsp")

add_library(dsrc_dsp SHARED
    dsrc_dsp.cpp
)

find_library(log-lib log)
target_link_libraries(dsrc_dsp ${log-lib})
