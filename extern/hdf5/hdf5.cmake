
include (ExternalProject)

set (name hdf5)
set (source_dir ${CMAKE_CURRENT_BINARY_DIR}/${name}/source)
set (install_dir ${CMAKE_CURRENT_BINARY_DIR}/${name}/install)

set (hdf5_urls
    https://github.com/HDFGroup/hdf5/archive/refs/tags/hdf5_1.14.4.tar.gz
)
vsag_resolve_thirdparty_override (HDF5 hdf5_1.14.4 hdf5_urls)
vsag_resolve_prebuilt (hdf5 hdf5_1.14.4 MD5=fdea52afcce07ed6c3e2a36e7fa11f21
                      "${VSAG_THIRDPARTY_CXX_FLAGS}" install_dir hdf5_prebuilt)
set (HDF5_CPP_STATIC_LIBRARY ${install_dir}/lib/libhdf5_cpp.a)
set (HDF5_C_STATIC_LIBRARY ${install_dir}/lib/libhdf5.a)

if (hdf5_prebuilt)
    add_custom_target (${name})
else ()
    ExternalProject_Add (
        ${name}
        URL ${hdf5_urls}
        URL_HASH MD5=fdea52afcce07ed6c3e2a36e7fa11f21
        DOWNLOAD_NAME hdf5_1.14.4.tar.gz
        PREFIX ${CMAKE_CURRENT_BINARY_DIR}/${name}
        TMP_DIR ${BUILD_INFO_DIR}
        STAMP_DIR ${BUILD_INFO_DIR}
        DOWNLOAD_DIR ${DOWNLOAD_DIR}
        SOURCE_DIR ${source_dir}
        CONFIGURE_COMMAND
            cmake ${common_cmake_args} -DHDF5_ENABLE_NONSTANDARD_FEATURE_FLOAT16=OFF
            -DCMAKE_INSTALL_PREFIX=${install_dir} -DHDF5_BUILD_CPP_LIB=ON
            -DBUILD_STATIC_LIBS=ON -DBUILD_SHARED_LIBS=OFF -DBUILD_TESTING=OFF
            -DHDF5_BUILD_EXAMPLES=OFF -DHDF5_BUILD_TOOLS=OFF -DHDF5_BUILD_HL_LIB=OFF
            -S. -Bbuild
        BUILD_COMMAND
            cmake --build build --parallel ${NUM_BUILDING_JOBS}
        INSTALL_COMMAND
            cmake --install build
        BUILD_IN_SOURCE 1
        LOG_CONFIGURE TRUE
        LOG_BUILD TRUE
        LOG_INSTALL TRUE
        DOWNLOAD_NO_PROGRESS 1
        INACTIVITY_TIMEOUT 5
        TIMEOUT 30

        BUILD_BYPRODUCTS
            ${HDF5_CPP_STATIC_LIBRARY}
            ${HDF5_C_STATIC_LIBRARY}
    )
endif ()

add_library (vsag_hdf5_headers INTERFACE)
target_include_directories (vsag_hdf5_headers INTERFACE ${install_dir}/include)
