# SPDX-License-Identifier: Apache-2.0
cmake_minimum_required(VERSION 3.16)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(esp32-classic-peer)

# Reading the clock, or the AFH map a link hops over, means sending an HCI
# command Bluedroid has no call for, and the queue it has to go on is private
# to ESP-IDF's "bt" component. So those source files are built as part of that
# component, which gives them the component's own include path. ESP-IDF's own
# tree is not touched.
idf_component_get_property(utesp_bt_lib bt COMPONENT_LIB)
target_sources(${utesp_bt_lib} PRIVATE "${CMAKE_CURRENT_LIST_DIR}/bluedroid/utesp_clock.c"
                                       "${CMAKE_CURRENT_LIST_DIR}/bluedroid/utesp_afh.c")
target_include_directories(${utesp_bt_lib} PRIVATE "${CMAKE_CURRENT_LIST_DIR}/bluedroid")
