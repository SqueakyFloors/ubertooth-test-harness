# SPDX-License-Identifier: Apache-2.0
cmake_minimum_required(VERSION 3.16)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(esp32-classic-peer)

# Reading the clock means sending an HCI command Bluedroid has no call for,
# and the queue it has to go on is private to ESP-IDF's "bt" component. So
# that one source file is built as part of that component, which gives it the
# component's own include path. ESP-IDF's own tree is not touched.
idf_component_get_property(utesp_bt_lib bt COMPONENT_LIB)
target_sources(${utesp_bt_lib} PRIVATE "${CMAKE_CURRENT_LIST_DIR}/bluedroid/utesp_clock.c")
