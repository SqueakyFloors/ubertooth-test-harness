
# Consider dependencies only in project.
set(CMAKE_DEPENDS_IN_PROJECT_ONLY OFF)

# The set of languages for which implicit dependencies are needed:
set(CMAKE_DEPENDS_LANGUAGES
  "ASM"
  )
# The set of files for implicit dependencies of each language:
set(CMAKE_DEPENDS_CHECK_ASM
  "/Users/ruswagne/esp/esp-idf/components/xtensa/xtensa_context.S" "/Users/ruswagne/worktrees/ubertooth-test-harness/88_esp32-clock-wt/firmware/esp32-classic-peer/build/esp-idf/xtensa/CMakeFiles/__idf_xtensa.dir/xtensa_context.S.obj"
  "/Users/ruswagne/esp/esp-idf/components/xtensa/xtensa_intr_asm.S" "/Users/ruswagne/worktrees/ubertooth-test-harness/88_esp32-clock-wt/firmware/esp32-classic-peer/build/esp-idf/xtensa/CMakeFiles/__idf_xtensa.dir/xtensa_intr_asm.S.obj"
  "/Users/ruswagne/esp/esp-idf/components/xtensa/xtensa_vectors.S" "/Users/ruswagne/worktrees/ubertooth-test-harness/88_esp32-clock-wt/firmware/esp32-classic-peer/build/esp-idf/xtensa/CMakeFiles/__idf_xtensa.dir/xtensa_vectors.S.obj"
  )
set(CMAKE_ASM_COMPILER_ID "GNU")

# Preprocessor definitions for this target.
set(CMAKE_TARGET_DEFINITIONS_ASM
  "ESP_PLATFORM"
  [[IDF_VER="v5.5.6"]]
  "SOC_MMU_PAGE_SIZE=CONFIG_MMU_PAGE_SIZE"
  "SOC_XTAL_FREQ_MHZ=CONFIG_XTAL_FREQ"
  "_GLIBCXX_HAVE_POSIX_SEMAPHORE"
  "_GLIBCXX_USE_POSIX_SEMAPHORE"
  "_GNU_SOURCE"
  "_POSIX_READER_WRITER_LOCKS"
  )

# The include file search paths:
set(CMAKE_ASM_TARGET_INCLUDE_PATH
  "config"
  "/Users/ruswagne/esp/esp-idf/components/xtensa/esp32/include"
  "/Users/ruswagne/esp/esp-idf/components/xtensa/include"
  "/Users/ruswagne/esp/esp-idf/components/xtensa/deprecated_include"
  "/Users/ruswagne/esp/esp-idf/components/newlib/platform_include"
  "/Users/ruswagne/esp/esp-idf/components/freertos/config/include"
  "/Users/ruswagne/esp/esp-idf/components/freertos/config/include/freertos"
  "/Users/ruswagne/esp/esp-idf/components/freertos/config/xtensa/include"
  "/Users/ruswagne/esp/esp-idf/components/freertos/FreeRTOS-Kernel/include"
  "/Users/ruswagne/esp/esp-idf/components/freertos/FreeRTOS-Kernel/portable/xtensa/include"
  "/Users/ruswagne/esp/esp-idf/components/freertos/FreeRTOS-Kernel/portable/xtensa/include/freertos"
  "/Users/ruswagne/esp/esp-idf/components/freertos/esp_additions/include"
  "/Users/ruswagne/esp/esp-idf/components/esp_hw_support/include"
  "/Users/ruswagne/esp/esp-idf/components/esp_hw_support/include/soc"
  "/Users/ruswagne/esp/esp-idf/components/esp_hw_support/include/soc/esp32"
  "/Users/ruswagne/esp/esp-idf/components/esp_hw_support/dma/include"
  "/Users/ruswagne/esp/esp-idf/components/esp_hw_support/ldo/include"
  "/Users/ruswagne/esp/esp-idf/components/esp_hw_support/debug_probe/include"
  "/Users/ruswagne/esp/esp-idf/components/esp_hw_support/mspi_timing_tuning/include"
  "/Users/ruswagne/esp/esp-idf/components/esp_hw_support/mspi_timing_tuning/tuning_scheme_impl/include"
  "/Users/ruswagne/esp/esp-idf/components/esp_hw_support/power_supply/include"
  "/Users/ruswagne/esp/esp-idf/components/esp_hw_support/port/esp32/."
  "/Users/ruswagne/esp/esp-idf/components/esp_hw_support/port/esp32/include"
  "/Users/ruswagne/esp/esp-idf/components/heap/include"
  "/Users/ruswagne/esp/esp-idf/components/heap/tlsf"
  "/Users/ruswagne/esp/esp-idf/components/log/include"
  "/Users/ruswagne/esp/esp-idf/components/soc/include"
  "/Users/ruswagne/esp/esp-idf/components/soc/esp32"
  "/Users/ruswagne/esp/esp-idf/components/soc/esp32/include"
  "/Users/ruswagne/esp/esp-idf/components/soc/esp32/register"
  "/Users/ruswagne/esp/esp-idf/components/hal/platform_port/include"
  "/Users/ruswagne/esp/esp-idf/components/hal/esp32/include"
  "/Users/ruswagne/esp/esp-idf/components/hal/include"
  "/Users/ruswagne/esp/esp-idf/components/esp_rom/include"
  "/Users/ruswagne/esp/esp-idf/components/esp_rom/esp32/include"
  "/Users/ruswagne/esp/esp-idf/components/esp_rom/esp32/include/esp32"
  "/Users/ruswagne/esp/esp-idf/components/esp_rom/esp32"
  "/Users/ruswagne/esp/esp-idf/components/esp_common/include"
  "/Users/ruswagne/esp/esp-idf/components/esp_system/include"
  "/Users/ruswagne/esp/esp-idf/components/esp_system/port/soc"
  "/Users/ruswagne/esp/esp-idf/components/esp_system/port/include/private"
  "/Users/ruswagne/esp/esp-idf/components/lwip/include"
  "/Users/ruswagne/esp/esp-idf/components/lwip/include/apps"
  "/Users/ruswagne/esp/esp-idf/components/lwip/include/apps/sntp"
  "/Users/ruswagne/esp/esp-idf/components/lwip/lwip/src/include"
  "/Users/ruswagne/esp/esp-idf/components/lwip/port/include"
  "/Users/ruswagne/esp/esp-idf/components/lwip/port/freertos/include"
  "/Users/ruswagne/esp/esp-idf/components/lwip/port/esp32xx/include"
  "/Users/ruswagne/esp/esp-idf/components/lwip/port/esp32xx/include/arch"
  "/Users/ruswagne/esp/esp-idf/components/lwip/port/esp32xx/include/sys"
  )

# The set of dependency files which are needed:
set(CMAKE_DEPENDS_DEPENDENCY_FILES
  "/Users/ruswagne/esp/esp-idf/components/xtensa/eri.c" "esp-idf/xtensa/CMakeFiles/__idf_xtensa.dir/eri.c.obj" "gcc" "esp-idf/xtensa/CMakeFiles/__idf_xtensa.dir/eri.c.obj.d"
  "/Users/ruswagne/esp/esp-idf/components/xtensa/xt_trax.c" "esp-idf/xtensa/CMakeFiles/__idf_xtensa.dir/xt_trax.c.obj" "gcc" "esp-idf/xtensa/CMakeFiles/__idf_xtensa.dir/xt_trax.c.obj.d"
  "/Users/ruswagne/esp/esp-idf/components/xtensa/xtensa_intr.c" "esp-idf/xtensa/CMakeFiles/__idf_xtensa.dir/xtensa_intr.c.obj" "gcc" "esp-idf/xtensa/CMakeFiles/__idf_xtensa.dir/xtensa_intr.c.obj.d"
  )

# Targets to which this target links which contain Fortran sources.
set(CMAKE_Fortran_TARGET_LINKED_INFO_FILES
  )

# Targets to which this target links which contain Fortran sources.
set(CMAKE_Fortran_TARGET_FORWARD_LINKED_INFO_FILES
  )

# Fortran module output directory.
set(CMAKE_Fortran_TARGET_MODULE_DIR "")
