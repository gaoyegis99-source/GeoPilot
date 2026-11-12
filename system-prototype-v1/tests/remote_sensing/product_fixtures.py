"""Synthetic native XML and real GeoTIFF inputs for remote-sensing tests."""

import numpy as np
import rasterio
from rasterio.transform import from_origin


DOMAIN = "GEOPILOT_PRODUCT"
LANDSAT = "landsat-c2-level-2"
SENTINEL = "sentinel-2-level-2a"
LANDSAT_CODES = ("QA_PIXEL", "SR_B5", "QA_RADSAT", "SR_B4")
SENTINEL_CODES = ("SCL", "B08", "B04")
GRANULE = "S2A_OPER_MSI_L2A_TL_2APS_20240831T151747_A048010_T32UMA_N05.11"
OTHER_GRANULE = "S2A_OPER_MSI_L2A_TL_2APS_20240831T151747_A048010_T32UMB_N05.11"


def build_landsat_xml(*, spacecraft="LANDSAT_8", level="L2SP", collection="02"):
    """Synthetic XML excerpt with native Collection 2 group and field names."""
    product = f"LC0{spacecraft[-1]}_{level}_043035_20241101_20241103_{collection}_T1"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<LANDSAT_METADATA_FILE>
  <PRODUCT_CONTENTS>
    <LANDSAT_PRODUCT_ID>{product}</LANDSAT_PRODUCT_ID>
    <PROCESSING_LEVEL>{level}</PROCESSING_LEVEL>
    <COLLECTION_NUMBER>{collection}</COLLECTION_NUMBER>
    <FILE_NAME_BAND_4>{product}_SR_B4.TIF</FILE_NAME_BAND_4>
    <FILE_NAME_BAND_5>{product}_SR_B5.TIF</FILE_NAME_BAND_5>
    <FILE_NAME_QUALITY_L1_PIXEL>{product}_QA_PIXEL.TIF</FILE_NAME_QUALITY_L1_PIXEL>
    <FILE_NAME_QUALITY_L1_RADIOMETRIC_SATURATION>{product}_QA_RADSAT.TIF</FILE_NAME_QUALITY_L1_RADIOMETRIC_SATURATION>
  </PRODUCT_CONTENTS>
  <IMAGE_ATTRIBUTES>
    <SPACECRAFT_ID>{spacecraft}</SPACECRAFT_ID>
    <SENSOR_ID>OLI_TIRS</SENSOR_ID>
  </IMAGE_ATTRIBUTES>
  <LEVEL2_PROCESSING_RECORD>
    <LANDSAT_PRODUCT_ID>{product}</LANDSAT_PRODUCT_ID>
    <PROCESSING_LEVEL>{level}</PROCESSING_LEVEL>
    <PROCESSING_SOFTWARE_VERSION>LPGS_16.4.0</PROCESSING_SOFTWARE_VERSION>
  </LEVEL2_PROCESSING_RECORD>
  <LEVEL2_SURFACE_REFLECTANCE_PARAMETERS>
    <REFLECTANCE_MULT_BAND_4>2.75E-05</REFLECTANCE_MULT_BAND_4>
    <REFLECTANCE_ADD_BAND_4>-0.2</REFLECTANCE_ADD_BAND_4>
    <REFLECTANCE_MULT_BAND_5>2.75E-05</REFLECTANCE_MULT_BAND_5>
    <REFLECTANCE_ADD_BAND_5>-0.2</REFLECTANCE_ADD_BAND_5>
  </LEVEL2_SURFACE_REFLECTANCE_PARAMETERS>
  <LEVEL1_PROCESSING_RECORD>
    <FILE_NAME_BAND_4>unrelated_L1_B4.TIF</FILE_NAME_BAND_4>
  </LEVEL1_PROCESSING_RECORD>
  <LEVEL1_RADIOMETRIC_RESCALING>
    <REFLECTANCE_MULT_BAND_4>1.0</REFLECTANCE_MULT_BAND_4>
  </LEVEL1_RADIOMETRIC_RESCALING>
</LANDSAT_METADATA_FILE>"""


def build_sentinel_granule_xml(granule=GRANULE, tile="T32UMA"):
    source = f"GRANULE/L2A_{tile}_A048010_20240831T103023/IMG_DATA"
    files = (("B04", 20), ("B8A", 20), ("B08", 10), ("SCL", 60), ("B04", 10), ("SCL", 20))
    entries = "\n".join(
        f"<IMAGE_FILE>{source}/R{size}m/{tile}_20240831T103021_{code}_{size}m</IMAGE_FILE>"
        for code, size in files
    )
    return f'<Granule granuleIdentifier="{granule}" imageFormat="JPEG2000">{entries}</Granule>'


def build_sentinel_xml(*, baseline="05.11", granules=None):
    """Synthetic XML excerpt using the original PB 05.11 mixed namespaces."""
    entries = build_sentinel_granule_xml() if granules is None else granules
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<n1:Level-2A_User_Product xmlns:n1="https://psd-15.sentinel2.eo.esa.int/PSD/User_Product_Level-2A.xsd">
  <n1:General_Info>
    <Product_Info>
      <PRODUCT_URI>S2A_MSIL2A_20240831T103021_N{baseline.replace('.', '')}_R108_T32UMA_20240831T151747.SAFE</PRODUCT_URI>
      <PROCESSING_LEVEL>Level-2A</PROCESSING_LEVEL>
      <PRODUCT_TYPE>S2MSI2A</PRODUCT_TYPE>
      <PROCESSING_BASELINE>{baseline}</PROCESSING_BASELINE>
      <Datatake><SPACECRAFT_NAME>Sentinel-2A</SPACECRAFT_NAME></Datatake>
      <Product_Organisation><Granule_List>{entries}</Granule_List></Product_Organisation>
    </Product_Info>
    <Product_Image_Characteristics>
      <QUANTIFICATION_VALUES_LIST><BOA_QUANTIFICATION_VALUE unit="none">10000</BOA_QUANTIFICATION_VALUE></QUANTIFICATION_VALUES_LIST>
      <BOA_ADD_OFFSET_VALUES_LIST>
        <BOA_ADD_OFFSET band_id="8">-999</BOA_ADD_OFFSET>
        <BOA_ADD_OFFSET band_id="7">-1000</BOA_ADD_OFFSET>
        <BOA_ADD_OFFSET band_id="3">-1000</BOA_ADD_OFFSET>
      </BOA_ADD_OFFSET_VALUES_LIST>
      <Spectral_Information_List>
        <Spectral_Information bandId="3" physicalBand="B4"><RESOLUTION>10</RESOLUTION></Spectral_Information>
        <Spectral_Information bandId="7" physicalBand="B8"><RESOLUTION>10</RESOLUTION></Spectral_Information>
        <Spectral_Information bandId="8" physicalBand="B8A"><RESOLUTION>20</RESOLUTION></Spectral_Information>
      </Spectral_Information_List>
    </Product_Image_Characteristics>
  </n1:General_Info>
</n1:Level-2A_User_Product>"""


def write_stack(
    path, *, profile, xml, codes, pixel_m, granule=None, scl_resampling=None,
    nodata=None, dtype="uint16", scales=None, shared_mask=None, crs="EPSG:32632", values=None,
):
    """Write real raw DN bands in the requested physical order."""
    if values is None:
        values = np.zeros((len(codes), 2, 2), dtype=dtype)
        for index, code in enumerate(codes):
            values[index, 0, 1] = 4 if code == "SCL" else 8 if code.startswith("QA_") else 15000
    values = np.asarray(values, dtype=dtype)
    if values.ndim != 3 or values.shape[0] != len(codes):
        raise ValueError("Fixture values must have one raster array per supplied band description")
    count, height, width = values.shape
    with rasterio.open(
        path, "w", driver="GTiff", width=width, height=height, count=count, dtype=dtype,
        crs=crs, transform=from_origin(500000, 5100000, pixel_m, pixel_m), nodata=nodata,
    ) as dataset:
        dataset.write(values)
        if scales is not None:
            dataset.scales = scales
        if shared_mask is not None:
            dataset.write_mask(shared_mask)
        raw_tags = {"PROFILE": profile, "METADATA_XML": xml, "GRANULE_ID": granule}
        tags = {key: value for key, value in raw_tags.items() if value is not None}
        dataset.update_tags(ns=DOMAIN, **tags)
        for index, code in enumerate(codes, start=1):
            dataset.set_band_description(index, code)
            if code == "SCL" and scl_resampling is not None:
                dataset.update_tags(index, ns=DOMAIN, RESAMPLING=scl_resampling)
    return path
