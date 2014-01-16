# Filename: PreHPCC.py
# Purpose: Mosaic NEDs to NHD subregions, burn streams and clip output to HUC8 boundaries.

import os, shutil
import arcpy
from arcpy.sa import *
from arcpy import env
import csiutils as cu



####################################################################################################################################################
# Mosiac NED tiles and clip to subregion.
def mosaic(nhd, nhdsubregion, nedfolder, subregion_buffer, subregion_ned, projection, in_memory, outfolder):
    # Set up environments
    arcpy.ResetEnvironments()
    env.overwriteOutput = True
    env.compression = "LZ77" # compress temp tifs for speed
    env.resample = "BILINEAR" # proper for elevation data
    env.pyramids = "NONE"
    env.outputCoordinateSystem = projection

    env.workspace = nhd

    # Select the right HUC4 from WBD_HU4 and make it it's own layer.
    whereClause = ''' "%s" = '%s' ''' % ("HUC_4", nhdsubregion)
    arcpy.MakeFeatureLayer_management("WBD_HU4", "Subregion", whereClause)

    # Apply a 5000 meter buffer around subregion
    arcpy.Buffer_analysis("Subregion", subregion_buffer, "5000 meters")
    cu.multi_msg("Buffered subregion.")

    # Walk through the folder with NEDs to make a list of rasters
    mosaicrasters = []
    for dirpath, dirnames, filenames in arcpy.da.Walk(nedfolder, datatype="RasterDataset"):
        for filename in filenames:
            name = os.path.join(dirpath, filename)
            mosaicrasters.append(name)

    cu.multi_msg("Found NED ArcGrids.")

    # Update environments
    env.extent = subregion_buffer
    if in_memory:
        env.workspace = 'in_memory'
        raster_extension = ''
    else:
        env.workspace = outfolder
        raster_extension = '.tif'

    # Assign names to intermediate outputs in outfolder
    mosaic_unproj = "mosaic_t1" + raster_extension
    mosaic_proj = "mosaic_t2" + raster_extension

    # Mosaic, then project
    # Cannot do this in one step using MosaicToNewRaster's projection parameter
    # because you cannot set the cell size correctly
    cu.multi_msg("Creating initial mosaic. This may take a while...")

    arcpy.MosaicToNewRaster_management(mosaicrasters, env.workspace,
    mosaic_unproj, "", "32_BIT_FLOAT", "", "1", "LAST")

    cu.multi_msg("Projecting mosaic...")

    arcpy.ProjectRaster_management(mosaic_unproj, mosaic_proj,
    projection, "BILINEAR", "10")

    #final mosaic environs, may be needed with TauDEM so uncompressed
    env.compression = "NONE"
    env.pyramids = "PYRAMIDS -1 SKIP_FIRST" # need to check outputs efficiently
    cu.multi_msg("Clipping final mosaic...")

    arcpy.Clip_management(mosaic_proj, '', subregion_ned, subregion_buffer,
     "0", "ClippingGeometry")

    # Clean up
    cu.cleanup([mosaic_unproj, mosaic_proj])
    cu.multi_msg("Mosaicked NED tiles and clipped to HUC4 extent.")

# END OF DEF mosaic

#################################################################################################################################################
# Burning Streams

def burn(subregion_ned, subregion_buffer, nhd, projection, burnt_ned, in_memory, outfolder):
    arcpy.ResetEnvironments()
    env.overwriteOutput = "TRUE"
    env.extent = subregion_buffer
    env.snapRaster = subregion_ned
    env.outputCoordinateSystem = projection
    env.compression = "LZ77" # compress temp tifs for speed

    env.workspace = outfolder

    # Copy flowlines to shapefile that will inherit environ output coord system
    arcpy.FeatureClassToShapefile_conversion(os.path.join(nhd, "NHDFlowline"), outfolder)
    flow_line = "NHDFlowline.shp"

    cu.multi_msg("Prepared NHDFlowline for rasterizing.")

    # Feature to Raster- rasterize the NHDFlowline
    flow_line_raster = "flow_line_raster.tif"
    arcpy.FeatureToRaster_conversion(flow_line, "FID", flow_line_raster, "10")
    cu.multi_msg("Converted flowlines to raster.")

    # Raster Calculator- burns in streams, beveling in from 500m
    cu.multi_msg("Burning streams into raster. This may take a while....")
    distance = EucDistance(flow_line, cell_size = "10")
    streams = Reclassify(Raster(flow_line_raster) > 0, "Value", "1 1; NoData 0")
    burnt = Raster(subregion_ned) - (10 * streams) - (0.02 * (500 - distance) * (distance < 500))
    burnt.save(burnt_ned)
    cu.multi_msg("Burned the streams into the NED, 10m deep and beveling in from 500m out.")

    # Delete intermediate rasters and shapefiles
    cu.cleanup([flow_line, flow_line_raster])
    cu.multi_msg("Burn process completed")

###############################################################################################################################################

def clip(raster, nhd, nhdsubregion, projection, outfolder):

    arcpy.ResetEnvironments()
    env.overwriteOutput = "TRUE"
    arcpy.env.workspace = nhd
    arcpy.env.outputCoordinateSystem = projection
    arcpy.env.compression = "NONE" # only final tifs are generated
    arcpy.env.pyramid = "NONE"

    # Create a feature dataset in NHD file geodatabase named "HUC8_Albers" in Albers projection
    out_feature_dataset = "HUC8_Albers"
    arcpy.CreateFeatureDataset_management(env.workspace, out_feature_dataset, projection)
    arcpy.RefreshCatalog(nhd)

    # HUC8 polygons each saved as separate fc inheriting albers from environ
    huc8_fc = "WBD_HU8"
    field = "HUC_8"
    arcpy.MakeFeatureLayer_management(huc8_fc, "huc8_layer")

    with arcpy.da.SearchCursor(huc8_fc, field) as cursor:
        for row in cursor:
            if row[0].startswith(nhdsubregion):
                whereClause = ''' "%s" = '%s' ''' % (field, row[0])
                arcpy.SelectLayerByAttribute_management("huc8_layer", 'NEW_SELECTION', whereClause)
                arcpy.CopyFeatures_management("huc8_layer", os.path.join(out_feature_dataset, "HUC" + row[0]))

    #retrieve only the single huc8 fcs and not the one with all of them
    fcs = arcpy.ListFeatureClasses("HUC%s*" % nhdsubregion, "Polygon", out_feature_dataset)
    fcs_buffered = [os.path.join(out_feature_dataset, fc + "_buffer") for fc in fcs]
    out_clips = [os.path.join(outfolder, "huc8clips" + nhdsubregion,
    "NED" + fc[3:] + ".tif") for fc in fcs]

    # Buffer HUC8 feature classes by 5000m
    for fc, fc_buffered in zip(fcs, fcs_buffered):
        arcpy.Buffer_analysis(fc, fc_buffered, "5000 meters")

    cu.multi_msg("Created HUC8 buffers.")
    arcpy.RefreshCatalog(nhd)

    # Clips rasters
    cu.multi_msg("Starting HUC8 clips...")
    for fc_buffered, out_clip in zip(fcs_buffered, out_clips):
        arcpy.Clip_management(raster, '', out_clip, fc_buffered, "0", "ClippingGeometry")

    arcpy.Compact_management(nhd)

    cu.multi_msg("Clipping complete.")

#END OF DEF clip

def is_inmemory(allowed_size, input_directory):
    original_size = cu.directory_size(input_directory)
    if original_size < allowed_size:
        return True
    else:
        return False

# "Output" is mosaic with file path = subregion_ned
def main():
    # Defaults on optional tools is an empty string ''
    nhd = arcpy.GetParameterAsText(0)          # NHD subregion file geodatabase
    nedfolder = arcpy.GetParameterAsText(1)    # Folder containing NED ArcGrids
    outfolder = arcpy.GetParameterAsText(2)    # Output folder
    input_mosaic = arcpy.GetParameterAsText(3) # Optional: mosaic from previous run
    input_burnt = arcpy.GetParameterAsText(4)  # Optional: 'burnt' NED from previous run

    # Naming conventions
    subregion_number = os.path.basename(nhd)
    nhdsubregion = subregion_number[4:8]
    mosaicfolder = os.path.join(outfolder, "mosaic" + nhdsubregion)
    burntfolder = os.path.join(outfolder, "streamsburnt")
    clipsfolder = os.path.join(outfolder, "huc8clips" + nhdsubregion)

    if input_mosaic:
        subregion_ned = input_mosaic
    else:
        subregion_ned = os.path.join(mosaicfolder, "NED13_" + nhdsubregion + ".tif")

    if input_burnt:
        burnt_ned = input_burnt
    else:
        burnt_ned = os.path.join(burntfolder, "Burnt_" + nhdsubregion + ".tif")
    subregion_buffer = os.path.join(nhd, "Subregion_5000m_buffer")

    # Create output directory tree
    for folder in [mosaicfolder, burntfolder, clipsfolder]:
        if not os.path.exists(folder):
            os.mkdir(folder)
    arcpy.RefreshCatalog(outfolder)

    # Create spatial reference objects:
    # NAD83 GCS (Input from NHD and NED)
    nad83 = arcpy.SpatialReference(4269)

    # USGS Albers (Our project's projection)
    albers = arcpy.SpatialReference(102039)

    # Determine whether to work in memory or not
    available_ram = 14 # in GB
    available_ram_bytes = available_ram * (1024 ** 3)
    ram_limit = available_ram_bytes/2
    in_memory = is_inmemory(ram_limit, nedfolder)

    arcpy.CheckOutExtension("Spatial")
    if not input_mosaic:
        mosaic(nhd, nhdsubregion, nedfolder, subregion_buffer, subregion_ned, albers, in_memory, outfolder)
    if not input_burnt:
        burn(subregion_ned, subregion_buffer, nhd, albers, burnt_ned, in_memory, outfolder)
    try:
        clip(burnt_ned, nhd, nhdsubregion, albers, outfolder)
        shutil.rmtree(burntfolder)
        cu.multi_msg("Complete. HUC8 burned clips are now ready for flow direction.")
    except arcpy.ExecuteError:
        cu.multi_msg("Clip failed, try again. Mosaic file is %s and burnt NED file is %s" %
        (subregion_ned, burnt_ned))
        arcpy.AddError(arcpy.GetMessages(2))
    except Exception as e:
        cu.multi_msg("Clip failed, try again. Mosaic file is %s and burnt NED file is %s" %
        (subregion_ned, burnt_ned))
        cu.multi_msg(e.message)

def test():
    # Defaults on optional tools is an empty string ''
    nhd = "C:/GISData/Scratch_njs/NHD0109/NHDH0109.gdb"          # NHD subregion file geodatabase
    nedfolder = "C:/GISData/Scratch_njs/NHD0109"   # Folder containing NED ArcGrids
    outfolder = "C:/GISData/Scratch_njs/PreHPCC"    # Output folder
    input_mosaic = '' # Optional: mosaic from previous run
    input_burnt = ''  # Optional: 'burnt' NED from previous run

    # Naming conventions
    subregion_number = os.path.basename(nhd)
    nhdsubregion = subregion_number[4:8]
    mosaicfolder = os.path.join(outfolder, "mosaic" + nhdsubregion)
    burntfolder = os.path.join(outfolder, "streamsburnt")
    clipsfolder = os.path.join(outfolder, "huc8clips" + nhdsubregion)

    if input_mosaic:
        subregion_ned = input_mosaic
    else:
        subregion_ned = os.path.join(mosaicfolder, "NED13_" + nhdsubregion + ".tif")

    if input_burnt:
        burnt_ned = input_burnt
    else:
        burnt_ned = os.path.join(burntfolder, "Burnt_" + nhdsubregion + ".tif")
    subregion_buffer = os.path.join(nhd, "Subregion_5000m_buffer")

    # Create output directory tree
    for folder in [mosaicfolder, burntfolder, clipsfolder]:
        if not os.path.exists(folder):
            os.mkdir(folder)
    arcpy.RefreshCatalog(outfolder)

    # Create spatial reference objects:
    # NAD83 GCS (Input from NHD and NED)
    nad83 = arcpy.SpatialReference(4269)

    # USGS Albers (Our project's projection)
    albers = arcpy.SpatialReference(102039)

# Determine whether to work in memory or not
    available_ram = 14 # in GB
    available_ram_bytes = available_ram * (1024 ** 3)
    ram_limit = available_ram_bytes/2
    in_memory = is_inmemory(ram_limit, nedfolder)

    arcpy.CheckOutExtension("Spatial")
    if not input_mosaic:
        mosaic(nhd, nhdsubregion, nedfolder, subregion_buffer, subregion_ned, albers, in_memory, outfolder)
    if not input_burnt:
        burn(subregion_ned, subregion_buffer, nhd, albers, burnt_ned, in_memory, outfolder)
    try:
        clip(burnt_ned, nhd, nhdsubregion, albers, outfolder)
        shutil.rmtree(burntfolder)
        cu.multi_msg("Complete. HUC8 burned clips are now ready for flow direction.")
    except arcpy.ExecuteError as e:
        cu.multi_msg("Clip failed, try again. Mosaic file is %s and burnt NED file is %s" %
        (subregion_ned, burnt_ned))
        cu.multi_msg(e.message)
    except Exception as e:
        cu.multi_msg("Clip failed, try again. Mosaic file is %s and burnt NED file is %s" %
        (subregion_ned, burnt_ned))
        cu.multi_msg(e.message)
    finally:
        arcpy.CheckInExtension("Spatial")

if __name__ == "__main__":
    main()