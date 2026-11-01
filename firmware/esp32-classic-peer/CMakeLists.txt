# SPDX-License-Identifier: Apache-2.0
cmake_minimum_required(VERSION 3.16)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(esp32-classic-peer)

# Reading the clock, or the AFH map a link hops over, means sending an HCI
# command Bluedroid has no call for, and the queue it has to go on is private
# to ESP-IDF's "bt" component. Choosing the synchronous packet type reaches
# two more of that component's private hooks. So these source files are built
# as part of that component, which gives them the component's own include
# path. ESP-IDF's own tree is not touched.
idf_component_get_property(utesp_bt_lib bt COMPONENT_LIB)
target_sources(${utesp_bt_lib} PRIVATE "${CMAKE_CURRENT_LIST_DIR}/bluedroid/utesp_clock.c"
                                       "${CMAKE_CURRENT_LIST_DIR}/bluedroid/utesp_afh.c"
                                       "${CMAKE_CURRENT_LIST_DIR}/bluedroid/utesp_sco_type.c")

# The board that accepts a synchronous link answers with the packet types it
# allows, and that list comes from a static const table with no override. So
# the one call that carries it to the controller is wrapped, and
# utesp_sco_type.c adjusts the list on its way past.
target_link_libraries(${utesp_bt_lib} INTERFACE "-Wl,--wrap=BTM_EScoConnRsp")
