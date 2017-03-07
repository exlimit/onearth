#!/bin/env python

# Copyright (c) 2002-2016, California Institute of Technology.
# All rights reserved.  Based on Government Sponsored Research under contracts NAS7-1407 and/or NAS7-03001.
#
# Redistribution and use in source and binary forms, with or without modification, are permitted provided that the following conditions are met:
#   1. Redistributions of source code must retain the above copyright notice, this list of conditions and the following disclaimer.
#   2. Redistributions in binary form must reproduce the above copyright notice,
#      this list of conditions and the following disclaimer in the documentation and/or other materials provided with the distribution.
#   3. Neither the name of the California Institute of Technology (Caltech), its operating division the Jet Propulsion Laboratory (JPL),
#      the National Aeronautics and Space Administration (NASA), nor the names of its contributors may be used to
#      endorse or promote products derived from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES,
# INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED.
# IN NO EVENT SHALL THE CALIFORNIA INSTITUTE OF TECHNOLOGY BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
# EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT,
# STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE,
# EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# 
# http://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
This script creates mod_reproject Apache configurations using a remote GetCapabilities file. It can be used on its own or
with oe_configure_layer.
"""

import sys
from lxml import etree
from oe_utils import log_sig_exit, log_sig_warn, log_info_mssg
import requests
import math
import functools
import os
import re
import shutil
from optparse import OptionParser

EARTH_RADIUS = 6378137.0
MIME_TO_EXTENSION = {
    'image/png': '.png',
    'image/jpeg': '.jpg',
    'image/tiff': '.tif',
    'image/lerc': '.lerc',
    'application/x-protobuf;type=mapbox-vector': '.pbf',
    'application/vnd.mapbox-vector-tile': '.mvt'
}


def sort_tilematrixset(tilematrixset):
    return sorted([elem for elem in tilematrixset.findall('{*}TileMatrix')], key=lambda matrix: float(matrix.findtext('{*}ScaleDenominator')))


def get_ext_for_file_type(file_type):
    try:
        return MIME_TO_EXTENSION[file_type]
    except KeyError:
        return None


def scale_denominator_reduce(previous, current, src_scale_denominator):
    current_scale_denominator = get_max_scale_dem(current)
    if previous is None:
        if current_scale_denominator > src_scale_denominator:
            return current
        else:
            return None
    previous_scale_denominator = get_max_scale_dem(previous)
    if current_scale_denominator < src_scale_denominator or current_scale_denominator > previous_scale_denominator:
        return previous
    return current


def get_max_scale_dem(tilematrixset):
    return float(sort_tilematrixset(tilematrixset)[0].findtext('{*}ScaleDenominator'))


def build_reproject_configs(layer_config_path, tilematrixsets_config_path, wmts=True, twms=True, create_gc=True, sigevent_url=None, debug=False, base_wmts_gc=None,
                            base_twms_gc=None, base_twms_get_tile_service=None):
    # Parse the config XML
    try:
        config_xml = etree.parse(layer_config_path)
    except IOError:
        log_sig_exit('ERROR', "Can't open reproject layer config file: {0}".format(layer_config_path), sigevent_url)
    except etree.XMLSyntaxError:
        log_sig_exit('ERROR', "Can't parse reproject layer config file: {0}".format(layer_config_path), sigevent_url)

    src_locations = config_xml.findall('SrcLocation')
    gc_uri = config_xml.findtext('GetCapabilitiesURI')
    if not gc_uri:
        log_sig_exit('ERROR: <GetCapabilitiesURI> not present in reprojection config file: {0}'.format(layer_config_path), sigevent_url)
    layer_exclude_list = [name.text for name in config_xml.findall('ExcludeLayer')]
    layer_include_list = [name.text for name in config_xml.findall('IncludeLayer')]

    # Parse the tilematrixsets definition file
    try:
        tilematrixset_defs_xml = etree.parse(tilematrixsets_config_path)
    except IOError:
        log_sig_exit('ERROR', "Can't open tile matrix sets config file: {0}".format(tilematrixsets_config_path), sigevent_url)
    except etree.XMLSyntaxError:
        log_sig_exit('ERROR', "Can't parse tile matrix sets config file: {0}".format(tilematrixsets_config_path), sigevent_url)

    # Parse the environment config and get required info
    environment_config_path = config_xml.findtext('EnvironmentConfig')
    if not environment_config_path:
        log_sig_exit('ERROR', "No environment configuration file specified: " + layer_config_path, sigevent_url)
    try:
        environment_xml = etree.parse(environment_config_path)
    except IOError:
        log_sig_exit('ERROR', "Can't open environment config file: {0}".format(environment_config_path), sigevent_url)
    except etree.XMLSyntaxError:
        log_sig_exit('ERROR', "Can't parse environment config file: {0}".format(environment_config_path), sigevent_url)
     
    wmts_staging_location = next(elem.text for elem in environment_xml.findall('StagingLocation') if elem.get('service') == 'wmts')
    if not wmts_staging_location:
        log_sig_warn('no wmts staging location specified', sigevent_url)

    twms_staging_location = next(elem.text for elem in environment_xml.findall('StagingLocation') if elem.get('service') == 'twms')
    if not twms_staging_location:
        log_sig_warn('no twms staging location specified', sigevent_url)

    wmts_reproject_endpoint = next(elem.text for elem in environment_xml.findall('ReprojectEndpoint') if elem.get('service') == 'wmts')
    if not wmts_reproject_endpoint:
        log_sig_warn('no wmts reproject endpoint specified -- cannot configure TWMS', sigevent_url)

    wmts_service_url = next(elem.text for elem in environment_xml.findall('ServiceURL') if elem.get('service') == 'wmts')
    if not wmts_service_url:
        log_sig_warn('no wmts service URL specified', sigevent_url)

    wmts_base_endpoint = next(elem.text for elem in environment_xml.findall('GetCapabilitiesLocation') if elem.get('service') == 'wmts')
    if not wmts_base_endpoint:
        log_sig_warn('no wmts GC URL specified', sigevent_url)

    wmts_apache_config_location_elem = next(elem for elem in environment_xml.findall('ApacheConfigLocation') if elem.get('service') == 'wmts')
    wmts_apache_config_location = wmts_apache_config_location_elem.text
    if not wmts_apache_config_location:
        log_sig_warn('no wmts Apache config location specified', sigevent_url)
    wmts_apache_config_basename = wmts_apache_config_location_elem.get('basename')
    if not wmts_apache_config_basename:
        log_sig_warn('no wmts Apache config basename specified', sigevent_url)

    wmts_apache_config_header_location_elem = next(elem for elem in environment_xml.findall('ApacheConfigHeaderLocation') if elem.get('service') == 'wmts')
    wmts_apache_config_header_location = wmts_apache_config_header_location_elem.text
    if not wmts_apache_config_header_location:
        log_sig_warn('no wmts Apache header location specified', sigevent_url)
    wmts_apache_config_header_basename = wmts_apache_config_header_location_elem.get('basename')
    if not wmts_apache_config_header_basename:
        log_sig_warn('no wmts Apache header basename specified', sigevent_url)

    twms_base_endpoint = next(elem.text for elem in environment_xml.findall('GetCapabilitiesLocation') if elem.get('service') == 'twms')
    if not twms_base_endpoint:
        log_sig_warn('no twms GC URL specified', sigevent_url)
    if os.path.split(os.path.dirname(twms_base_endpoint))[1] == '.lib':
        twms_base_endpoint = os.path.split(os.path.dirname(twms_base_endpoint))[0]

    twms_apache_config_location_elem = next(elem for elem in environment_xml.findall('ApacheConfigLocation') if elem.get('service') == 'twms')
    twms_apache_config_location = twms_apache_config_location_elem.text
    if not twms_apache_config_location:
        log_sig_warn('no twms Apache config location specified', sigevent_url)
    twms_apache_config_basename = twms_apache_config_location_elem.get('basename')
    if not twms_apache_config_basename:
        log_sig_warn('no twms Apache config basename specified', sigevent_url)

    twms_apache_config_header_location_elem = next(elem for elem in environment_xml.findall('ApacheConfigHeaderLocation') if elem.get('service') == 'twms')
    twms_apache_config_header_location = twms_apache_config_header_location_elem.text
    if not twms_apache_config_header_location:
        log_sig_warn('no twms Apache header location specified', sigevent_url)
    twms_apache_config_header_basename = twms_apache_config_header_location_elem.get('basename')
    if not twms_apache_config_header_basename:
        log_sig_warn('no twms Apache header basename specified', sigevent_url)

    wmts_service_url = next(elem.text for elem in environment_xml.findall('ServiceURL') if elem.get('service') == 'wmts')
    if not wmts_service_url and create_gc:
        log_sig_warn('no WMTS ServiceURL specified', sigevent_url)

    twms_service_url = next(elem.text for elem in environment_xml.findall('ServiceURL') if elem.get('service') == 'twms')
    if not twms_service_url and create_gc:
        log_sig_warn('no TWMS ServiceURL specified', sigevent_url)

    # Get all the TMSs for this projection
    available_tilematrixsets = []
    for tms_list in [elem.findall('{*}TileMatrixSet') for elem in tilematrixset_defs_xml.findall('{*}Projection') if elem.get('id') == 'EPSG:3857']:
        for tms in tms_list:
            available_tilematrixsets.append(tms)

    # Download and parse GC file from endpoint
    r = requests.get(gc_uri)
    if r.status_code != 200:
        log_sig_exit('ERROR', 'Can\'t download GetCapabilities from URL: ' + gc_uri, sigevent_url)

    # Get the layers and source TMSs from the source GC file
    try:
        gc_xml = etree.fromstring(r.content)
    except etree.XMLSyntaxError:
        log_sig_exit('ERROR', "Can't parse GetCapabilities file (invalid syntax): " + gc_uri, sigevent_url)
    gc_layers = gc_xml.find('{*}Contents').findall('{*}Layer')
    tilematrixsets = gc_xml.find('{*}Contents').findall('{*}TileMatrixSet')
    ows = '{' + gc_xml.nsmap['ows'] + '}'
    xlink = '{http://www.w3.org/1999/xlink}'

    for layer in gc_layers:
        identifier = layer.findtext(ows + 'Identifier')
        if debug:
            print 'Configuring layer: ' + identifier
        if identifier in layer_exclude_list:
            if debug:
                print 'Skipping layer: ' + identifier
            continue
        if layer_include_list and identifier not in layer_include_list:
            continue
        layer_endpoint = os.path.join(wmts_base_endpoint, identifier)
        layer_style_endpoint = os.path.join(layer_endpoint, 'default')
        layer_tms_apache_configs = []

        # Get TMSs for this layer and build a config for each
        tms_list = [elem for elem in tilematrixsets if elem.findtext(ows + 'Identifier') == layer.find('{*}TileMatrixSetLink').findtext('{*}TileMatrixSet')]
        layer_tilematrixsets = sorted(tms_list, key=get_max_scale_dem)
        out_tilematrixsets = []
        for tilematrixset in layer_tilematrixsets:
            # Start by getting the source info we need to configure this layer -- right now we are assuming geographic source projection
            src_tilematrixset_name = tilematrixset.findtext(ows + 'Identifier')
            matrices = sort_tilematrixset(tilematrixset)
            src_scale_denominator = float(matrices[0].findtext('{*}ScaleDenominator'))
            src_width = int(round(2 * math.pi * EARTH_RADIUS / (src_scale_denominator * 0.28E-3)))
            src_height = src_width / 2
            src_levels = len(matrices)
            src_pagesize_width = matrices[0].findtext('{*}TileWidth')
            src_pagesize_height = matrices[0].findtext('{*}TileHeight')
            src_bbox_elem = layer.find(ows + 'WGS84BoundingBox')
            src_bbox = ','.join(src_bbox_elem.findtext(ows + 'LowerCorner').split(' ')) + ',' + ','.join(src_bbox_elem.findtext(ows + 'UpperCorner').split(' '))

            # Now figure out the configuration for the destination layer.
            # Start by getting the output TileMatrixSet that most closely matches the scale denominator of the source.
            dest_tilematrixset = reduce(functools.partial(scale_denominator_reduce, src_scale_denominator=src_scale_denominator), available_tilematrixsets, None)
            out_tilematrixsets.append(dest_tilematrixset)
            dest_tilematrixset_name = dest_tilematrixset.findtext(ows + 'Identifier')
            dest_scale_denominator = float(sort_tilematrixset(dest_tilematrixset)[0].findtext('{*}ScaleDenominator'))
            dest_width = dest_height = int(round(2 * math.pi * EARTH_RADIUS / (dest_scale_denominator * 0.28E-3)))
            dest_levels = len(dest_tilematrixset.findall('{*}TileMatrix'))
            dest_pagesize_width = sort_tilematrixset(dest_tilematrixset)[0].findtext('{*}TileWidth')
            dest_pagesize_height = sort_tilematrixset(dest_tilematrixset)[0].findtext('{*}TileHeight')
            dest_top_left_corner = [float(value) for value in sort_tilematrixset(dest_tilematrixset)[0].findtext('{*}TopLeftCorner').split(' ')]
            dest_bbox = '{0},{1},{2},{3}'.format(dest_top_left_corner[0], dest_top_left_corner[0], -dest_top_left_corner[0], -dest_top_left_corner[0])
            dest_resource_url_elem = layer.find('{*}ResourceURL')
            dest_url = dest_resource_url_elem.get('template')
            for location in src_locations:
                dest_url = dest_url.replace(location.get('external'), location.get('internal'))
            dest_url = re.match('(.*default)', dest_url).group()
            dest_dim_elem = layer.find('{*}Dimension')
            if dest_dim_elem is not None and dest_dim_elem.findtext(ows + 'Identifier') == 'time':
                static = False
                dest_url = '{0}/{1}/{2}'.format(dest_url, '${date}', src_tilematrixset_name)
            else:
                static = True
                dest_url = '{0}/{1}'.format(dest_url, src_tilematrixset_name)
            dest_file_type = dest_resource_url_elem.get('format')
            dest_file_ext = get_ext_for_file_type(dest_file_type)
            if dest_file_ext == None:
                log_sig_warn("File type is not supported for OnEarth: " + dest_file_type, sigevent_url)
                break
            if dest_file_ext in ['.tif', '.lerc', '.mvt', '.pbf']:
                log_sig_warn("File type is not supported for reproject: " + dest_file_type, sigevent_url)
                break

            if wmts:
                # Write the source and reproject (output) configuration files
                wmts_staging_layer_path = os.path.join(wmts_staging_location, identifier)
                wmts_staging_style_path = os.path.join(wmts_staging_layer_path, "default")
                wmts_staging_path = os.path.join(wmts_staging_style_path, dest_tilematrixset_name)

                try:
                    os.makedirs(wmts_staging_path)
                except OSError:
                    if not os.path.exists(wmts_staging_path):
                        log_sig_exit('ERROR', 'WMTS staging location: ' + wmts_staging_path + ' cannot be created.', sigevent_url)
                    pass

                src_cfg_filename = identifier + '_source.config'
                with open(os.path.join(wmts_staging_path, src_cfg_filename), 'w+') as src_cfg:
                    src_cfg.write('Size {0} {1} {2}\n'.format(src_width, src_height, src_levels))
                    src_cfg.write('PageSize {0} {1}\n'.format(src_pagesize_width, src_pagesize_height))
                    src_cfg.write('Projection {0}\n'.format('EPSG:4326'))
                    src_cfg.write('SkippedLevels 1\n')
                    src_cfg.write('BoundingBox {0}\n'.format(src_bbox))

                dest_cfg_filename = identifier + '_reproject.config'
                with open(os.path.join(wmts_staging_path, dest_cfg_filename), 'w+') as dest_cfg:
                    dest_cfg.write('Size {0} {1} {2}\n'.format(dest_width, dest_height, dest_levels))
                    dest_cfg.write('PageSize {0} {1}\n'.format(dest_pagesize_width, dest_pagesize_height))
                    dest_cfg.write('Projection {0}\n'.format('EPSG:3857'))
                    dest_cfg.write('BoundingBox {0}\n'.format(dest_bbox))
                    dest_cfg.write('SourcePath {0}\n'.format(dest_url))
                    dest_cfg.write('SourcePostfix {0}\n'.format(dest_file_ext))
                    # dest_cfg.write('SkippedLevels 1\n')

                # Build Apache config snippet for TMS
                tms_path = os.path.join(layer_style_endpoint, dest_tilematrixset_name)
                tms_apache_config = '<Directory {0}>\n'.format(tms_path)
                tms_apache_config += '\tWMTSWrapperRole tilematrixset\n'
                regexp_str = dest_tilematrixset_name + '/\d{1,2}/\d{1,3}/\d{1,3}' + dest_file_ext
                tms_apache_config += '\tReproject_RegExp {0}\n'.format(regexp_str)
                tms_apache_config += '\tReproject_ConfigurationFiles {0} {1}\n'.format(os.path.join(tms_path, src_cfg_filename), os.path.join(tms_path, dest_cfg_filename))
                tms_apache_config += '</Directory>\n'
                layer_tms_apache_configs.append(tms_apache_config)

            # Build a TWMS configuration for the highest available TMS for this layer
            if twms and tilematrixset == layer_tilematrixsets[0]:
                reproj_twms_config_filename = 'twms.config'
                twms_staging_path = os.path.join(twms_staging_location, identifier)

                try:
                    os.makedirs(twms_staging_path)
                except OSError:
                    if not os.path.exists(twms_staging_path):
                        log_sig_exit('ERROR', 'TWMS staging location: ' + twms_staging_path + ' cannot be created.', sigevent_url)
                    pass

                with open(os.path.join(twms_staging_path, reproj_twms_config_filename), 'w+') as twms_cfg:
                    twms_cfg.write('Size {0} {1} {2}\n'.format(dest_width, dest_height, dest_levels))
                    twms_cfg.write('PageSize {0} {1}\n'.format(dest_pagesize_width, dest_pagesize_height))
                    twms_cfg.write('BoundingBox {0}\n'.format(dest_bbox))

                    # Build endpoint path for WMTS source
                    twms_src_layer_path = os.path.join(wmts_reproject_endpoint, identifier)
                    twms_src_style_path = os.path.join(twms_src_layer_path, 'default')
                    if static:
                        twms_src_path = os.path.join(twms_src_style_path, dest_tilematrixset_name)
                    else:
                        twms_src_time_path = os.path.join(twms_src_style_path, '${date}')
                        twms_src_path = os.path.join(twms_src_time_path, dest_tilematrixset_name)
                    twms_cfg.write('SourcePath {0}\n'.format(twms_src_path))
                    twms_cfg.write('SourcePostfix {0}'.format(dest_file_ext))
                    
        if dest_file_ext == None: # Skip layer if unsupported file type
            continue
        
        if wmts:
            # Finish building the layer Apache config
            layer_apache_config = '<Directory {0}>\n'.format(layer_endpoint)
            layer_apache_config += '\tWMTSWrapperRole layer\n'
            if not static:
                layer_apache_config += '\tWMTSWrapperEnableTime on\n'
            layer_apache_config += '</Directory>\n'
            layer_apache_config += '<Directory {0}>\n'.format(layer_style_endpoint)
            layer_apache_config += '\tWMTSWrapperRole style\n'
            layer_apache_config += '</Directory>\n'
            for layer_config in layer_tms_apache_configs:
                layer_apache_config += layer_config

            layer_apache_config_filename = identifier + 'reproject.conf'
            layer_apache_config_path = os.path.join(wmts_staging_location, layer_apache_config_filename)
            try:
                with open(layer_apache_config_path, 'w+') as f:
                    f.write(layer_apache_config)
            except IOError:
                log_sig_exit('ERROR', 'Cannot write layer config file: ' + layer_apache_config_path, sigevent_url)

            # Create final Apache configs (WMTS)
            endpoint_apache_config = '<Directory {0}>\n'.format(wmts_base_endpoint)
            endpoint_apache_config += '\tWMTSWrapperRole root\n'
            endpoint_apache_config += '</Directory>\n'
            apache_staging_conf_path = os.path.join(wmts_staging_location, wmts_apache_config_basename + '.conf')
            try:
                with open(apache_staging_conf_path, 'w+') as wmts_apache_config_file:
                    # Write header to Apache config
                    wmts_apache_header_path = os.path.join(wmts_apache_config_header_location, wmts_apache_config_header_basename + '.conf')
                    try:
                        with open(wmts_apache_header_path, 'r') as header:
                            wmts_apache_config_file.write(header.read())
                    except IOError:
                        log_sig_warn("Can't open WMTS reproject Apache header file: " + wmts_apache_header_path, sigevent_url)

                    # Write endpoint Apache stuff
                    wmts_apache_config_file.write(endpoint_apache_config)

                    # Write individual layer chunks
                    layer_snippets = [os.path.join(wmts_staging_location, sfile)
                              for sfile in sorted(os.listdir(wmts_staging_location), key=str.lower)
                              if sfile.endswith('.conf') and not sfile.startswith(wmts_apache_config_basename)]
                    for layer_path in layer_snippets:
                        with open(layer_path, 'r') as f:
                            wmts_apache_config_file.write(f.read())
            except IOError:
                log_sig_exit('ERROR', "Can't write WMTS staging apache config: " + apache_staging_conf_path, sigevent_url)

            # Modify layer GC xml for webmercator projection
            # Insert additional bounding box for WebMercator
            layer.find(ows + 'WGS84BoundingBox').find(ows + 'LowerCorner').text = '-180 -85.051129'
            layer.find(ows + 'WGS84BoundingBox').find(ows + 'UpperCorner').text = '180 85.051129'
            bb_elem_idx = layer.index(layer.find(ows + 'WGS84BoundingBox')) + 1
            bbox_elem = etree.Element(ows + 'BoundingBox', crs='urn:ogc:def:crs:EPSG::3857')
            bbox_upper_corner_elem = etree.Element(ows + 'UpperCorner')
            bbox_upper_corner_elem.text = '{0} {1}'.format(-dest_top_left_corner[0], -dest_top_left_corner[0])
            bbox_lower_corner_elem = etree.Element(ows + 'LowerCorner')
            bbox_lower_corner_elem.text = '{0} {1}'.format(dest_top_left_corner[0], dest_top_left_corner[0])
            bbox_elem.append(bbox_lower_corner_elem)
            bbox_elem.append(bbox_upper_corner_elem)
            layer.insert(bb_elem_idx, bbox_elem)

            # Add the TMSs we are using (clearing the ones from the source GC)
            tms_link_elem = layer.find('{*}TileMatrixSetLink')
            tms_link_elem.clear()
            for tms in out_tilematrixsets:
                tms_elem = etree.Element('TileMatrixSet')
                tms_elem.text = tms.findtext('{*}Identifier')
                tms_link_elem.append(tms_elem)

            # Modify the ResourceURL template
            base_url = wmts_service_url + identifier
            if static:
                wmts_service_template = base_url + '/default/{TileMatrixSet}/{TileMatrix}/{TileRow}/{TileCol}' + dest_file_ext
            else:
                wmts_service_template = base_url + '/default/{Time}/{TileMatrixSet}/{TileMatrix}/{TileRow}/{TileCol}' + dest_file_ext
            dest_resource_url_elem.set('template', wmts_service_template)

            wmts_gc_snippet_filename = '{0}_reproject.xml'.format(identifier)
            try:
                with open(os.path.join(wmts_staging_location, wmts_gc_snippet_filename), 'w+') as wmts_gc_snippet:
                    wmts_gc_snippet.write(etree.tostring(layer, pretty_print=True))
            except IOError:
                log_sig_exit('ERROR', 'Could not create staging XML snippet', sigevent_url)

    # Final routines (after all layers have been processed)
    if wmts:
        # Copy all the WMTS config files to the endpoint
        layer_dirs = [subdir for subdir in os.listdir(wmts_staging_location) if os.path.isdir(os.path.join(wmts_staging_location, subdir))]
        for layer_dir in layer_dirs:
            layer_endpoint = os.path.join(wmts_base_endpoint, layer_dir)
            if os.path.exists(layer_endpoint):
                shutil.rmtree(layer_endpoint)
            layer_staging_path = os.path.join(wmts_staging_location, layer_dir)
            print '\nCopying reprojected WMTS layer directories: {0} -> {1} '.format(layer_staging_path, layer_endpoint)
            shutil.copytree(layer_staging_path, layer_endpoint)

        # Copy the WMTS Apache config to the specified directory (like conf.d)
        apache_conf_path = os.path.join(wmts_apache_config_location, wmts_apache_config_basename + '.conf')
        print '\nCopying reprojected WMTS layer Apache config {0} -> {1}'.format(apache_staging_conf_path, apache_conf_path)
        shutil.copyfile(apache_staging_conf_path, apache_conf_path)

        # Configure GetCapabilties (routine copied from oe_configure_layer)
        try:
            # Copy and open base GetCapabilities.
            getCapabilities_file = os.path.join(wmts_staging_location, 'getCapabilities.xml')
            if not base_wmts_gc:
                log_sig_exit('ERROR', 'WMTS GetCapabilities creation selected but no base GC file specified.', sigevent_url)
            shutil.copyfile(base_wmts_gc, getCapabilities_file)
            getCapabilities_base = open(getCapabilities_file, 'r+')
        except IOError:
            log_sig_exit('ERROR', 'Cannot read getcapabilities_base_wmts.xml file: ' + base_wmts_gc, sigevent_url)
        else:
            lines = getCapabilities_base.readlines()
            for idx in range(0, len(lines)):
                if '<ows:Get' in lines[idx]:
                    spaces = lines[idx].index('<')
                    getUrlLine = lines[idx].replace('ows:Get','Get xmlns:xlink="http://www.w3.org/1999/xlink"').replace('>','/>')
                    getUrl = etree.fromstring(getUrlLine)
                    if '1.0.0/WMTSCapabilities.xml' in lines[idx]:
                        getUrl.attrib[xlink + 'href'] = wmts_service_url + '1.0.0/WMTSCapabilities.xml'
                    elif 'wmts.cgi?' in lines[idx]:
                        getUrl.attrib[xlink + 'href'] = wmts_service_url + 'wmts.cgi?'
                    else:
                        getUrl.attrib[xlink + 'href'] = wmts_service_url
                    lines[idx] = (' '*spaces) + etree.tostring(getUrl, pretty_print=True).replace('Get','ows:Get').replace(' xmlns:xlink="http://www.w3.org/1999/xlink"','').replace('/>','>')
                if 'ServiceMetadataURL' in lines[idx]:
                    spaces = lines[idx].index('<')
                    serviceMetadataUrlLine = lines[idx].replace('ServiceMetadataURL','ServiceMetadataURL xmlns:xlink="http://www.w3.org/1999/xlink"')
                    serviceMetadataUrl = etree.fromstring(serviceMetadataUrlLine)
                    serviceMetadataUrl.attrib[xlink + 'href'] = wmts_service_url + '1.0.0/WMTSCapabilities.xml'
                    lines[idx] = (' '*spaces) + etree.tostring(serviceMetadataUrl, pretty_print=True).replace(' xmlns:xlink="http://www.w3.org/1999/xlink"','')
            getCapabilities_base.seek(0)
            getCapabilities_base.truncate()
            getCapabilities_base.writelines(lines)
            getCapabilities_base.close()   

    if twms:
        # Create final Apache configs (TWMS)
        twms_apache_conf_path_template = os.path.join(twms_base_endpoint, '${layer}/twms.config')
        twms_endpoint_apache_config = '<Directory {0}>\n'.format(twms_base_endpoint)
        twms_endpoint_apache_config += '\ttWMS_RegExp twms.cgi\n'
        twms_endpoint_apache_config += '\ttWMS_ConfigurationFile {0}\n'.format(twms_apache_conf_path_template)
        twms_endpoint_apache_config += '</Directory>\n'
        twms_apache_staging_conf_path = os.path.join(twms_staging_location, twms_apache_config_basename + '.conf')
        try:
            with open(twms_apache_staging_conf_path, 'w+') as twms_apache_config_file:
                # Write header to Apache config
                try:
                    with open(os.path.join(twms_apache_config_header_location, twms_apache_config_header_basename + '.conf'), 'r') as header:
                        twms_apache_config_file.write(header.read())
                except IOError:
                    log_sig_warn("Can't find TWMS reproject Apache header file: " + twms_apache_staging_conf_path)

                # Write endpoint Apache stuff
                twms_apache_config_file.write(twms_endpoint_apache_config)

        except IOError:
            log_sig_exit('ERROR', "Can't write TWMS staging apache conf: " + twms_apache_staging_conf_path, sigevent_url)

        # Copy all the TWMS config files to the endpoint
        layer_dirs = [subdir for subdir in os.listdir(twms_staging_location) if os.path.isdir(os.path.join(twms_staging_location, subdir))]
        for layer_dir in layer_dirs:
            layer_endpoint = os.path.join(twms_base_endpoint, layer_dir)
            if os.path.exists(layer_endpoint):
                shutil.rmtree(layer_endpoint)
            layer_staging_path = os.path.join(twms_staging_location, layer_dir)
            print '\nCopying reprojected TWMS layer directories: {0} -> {1} '.format(layer_staging_path, layer_endpoint)
            shutil.copytree(layer_staging_path, layer_endpoint)

        # Copy the TWMS Apache config to the specified directory (like conf.d)
        twms_apache_conf_path = os.path.join(twms_apache_config_location, twms_apache_config_basename + '.conf')
        print '\nCopying reprojected TWMS layer Apache config {0} -> {1}'.format(twms_apache_staging_conf_path, twms_apache_conf_path)
        shutil.copyfile(twms_apache_staging_conf_path, twms_apache_conf_path)

        # Configure base GC file for TWMS
        if create_gc:
            try:
                # Copy and open base GetCapabilities.
                getCapabilities_file = os.path.join(twms_staging_location, 'getCapabilities.xml')
                if not base_twms_gc:
                    log_sig_exit('ERROR', 'TWMS GetCapabilities creation selected but no base GC file specified.', sigevent_url)
                shutil.copyfile(base_twms_gc, getCapabilities_file)
                getCapabilities_base = open(getCapabilities_file, 'r+')
            except IOError:
                log_sig_exit('ERROR', 'Cannot read getcapabilities_base_twms.xml file: ' + base_twms_gc, sigevent_url)
            else:
                lines = getCapabilities_base.readlines()
                for idx in range(0, len(lines)):
                    if '<SRS></SRS>' in lines[idx]:
                        lines[idx] =  lines[idx].replace('<SRS></SRS>', '<SRS>EPSG:3857</SRS>')
                    if '<CRS></CRS>' in lines[idx]:
                        lines[idx] =  lines[idx].replace('<CRS></CRS>', '<CRS>EPSG:3857</CRS>')
                    if 'OnlineResource' in lines[idx]:
                        spaces = lines[idx].index('<')
                        onlineResource = etree.fromstring(lines[idx])
                        if 'KeywordList' in lines[idx - 1]:
                            onlineResource.attrib[xlink + 'href'] = twms_service_url  # don't include the cgi portion
                        else:
                            onlineResource.attrib[xlink + 'href'] = twms_service_url + "twms.cgi?"
                        lines[idx] = (' '*spaces) + etree.tostring(onlineResource, pretty_print=True)
                getCapabilities_base.seek(0)
                getCapabilities_base.truncate()
                getCapabilities_base.writelines(lines)
                getCapabilities_base.close()

            try:
                # Copy and open base GetTileService.
                getTileService_file = os.path.join(twms_staging_location, 'getTileService.xml')
                if not base_twms_get_tile_service:
                    log_sig_exit('ERROR', 'TWMS GetTileService creation selected but no base GetTileService file specified.', sigevent_url)
                shutil.copyfile(base_twms_get_tile_service, getTileService_file)
                getTileService_base = open(getTileService_file, 'r+')
            except IOError:
                log_sig_exit('ERROR', 'Cannot read gettileservice_base.xml file: ' + getTileService_file, sigevent_url)
            else:
                lines = getTileService_base.readlines()
                for idx in range(0, len(lines)):
                    if 'BoundingBox' in lines[idx]:
                        lines[idx] = lines[idx].replace("BoundingBox","LatLonBoundingBox").replace("{minx}", '-20037508.34278925').replace("{miny}", '-20037508.34278925').replace("{maxx}", '20037508.34278925').replace("{maxy}", '20037508.34278925')
                    if 'OnlineResource' in lines[idx]:
                        spaces = lines[idx].index('<')
                        onlineResource = etree.fromstring(lines[idx])
                        if 'KeywordList' in lines[idx-1]:
                            onlineResource.attrib[xlink + 'href'] = twms_service_url  # don't include the cgi portion
                        else:
                            onlineResource.attrib[xlink + 'href'] = twms_service_url + "twms.cgi?"
                        lines[idx] = (' '*spaces) + etree.tostring(onlineResource, pretty_print=True)
                getTileService_base.seek(0)
                getTileService_base.truncate()
                getTileService_base.writelines(lines)
                getTileService_base.close()
    return

# Main routine to be run in CLI mode
if __name__ == '__main__':
    print 'OnEarth Reproject layer config tool (for use with mod_reproject)'

    if 'LCDIR' not in os.environ:
        print 'LCDIR environment variable not set.\nLCDIR should point to your OnEarth layer_config directory.\n'
        lcdir = os.path.abspath(os.path.dirname(__file__) + '/..')
    else:
        lcdir = os.environ['LCDIR']

    usageText = 'oe_configure_layer.py --conf_file [layer_configuration_file.xml] --lcdir [$LCDIR] --no_xml --sigevent_url [url] --no_twms --no_wmts'

    # Define command line options and args.
    parser = OptionParser(usage=usageText)
    parser.add_option('-c', '--conf_file',
                      action='store', type='string', dest='layer_config_path',
                      help='Full path of layer configuration filename.')
    parser.add_option('-l', '--lcdir',
                      action='store', type='string', dest='lcdir',
                      default=lcdir,
                      help='Full path of the OnEarth Layer Configurator (layer_config) directory.  Default: $LCDIR')
    parser.add_option('-m', '--tilematrixset_config',
                      action='store', type='string', dest='tilematrixsets_config_path',
                      help='Full path of TileMatrixSet configuration file.  Default: $LCDIR/conf/tilematrixsets.xml')
    parser.add_option("-n", "--no_twms",
                      action="store_true", dest="no_twms",
                      default=False, help="Do not use configurations for Tiled-WMS")
    parser.add_option('-s', '--sigevent_url',
                      action='store', type='string', dest='sigevent_url',
                      default='http://localhost:8100/sigevent/events/create',
                      help='Default:  http://localhost:8100/sigevent/events/create')
    parser.add_option("-w", "--no_wmts",
                      action="store_true", dest="no_wmts",
                      default=False, help="Do not use configurations for WMTS.")
    parser.add_option("-x", "--no_xml",
                      action="store_true", dest="no_xml",
                      default=False, help="Do not generate getCapabilities and getTileService XML.")
    parser.add_option("--debug",
                      action="store_true", dest="debug",
                      default=False, help="Produce verbose debug messages")

    # Read command line args.
    (options, args) = parser.parse_args()
    # Command line set LCDIR.
    lcdir = options.lcdir
    # No XML configurations (getCapabilities, getTileService)
    xml = not options.no_xml
    # No Tiled-WMS configuration.
    twms = not options.no_twms
    # No WMTS configuration.
    wmts = not options.no_wmts
    # TileMatrixSet configurations
    if options.tilematrixsets_config_path:
        tilematrixsets_config_path = options.tilematrixsets_config_path
    else:
        tilematrixsets_config_path = lcdir + '/conf/tilematrixsets.xml'

    layer_config_path = options.layer_config_path
    if not layer_config_path:
        print 'No layer config XML specified'
        sys.exit()

    # Sigevent URL.
    sigevent_url = options.sigevent_url

    base_twms_gc = os.path.join(lcdir, '/conf/getcapabilities_base_twms.xml')
    base_twms_get_tile_service = os.path.join(lcdir, '/conf/gettileservice_base.xml')
    base_wmts_gc = os.path.join(lcdir, '/conf/getcapabilities_base_wmts.xml')
      
    print 'Using ' + lcdir + ' as $LCDIR.'

    if not xml:
        log_info_mssg("no_xml specified, getCapabilities and getTileService files will be staged only")

    build_reproject_configs(layer_config_path, tilematrixsets_config_path, base_wmts_gc=base_wmts_gc, base_twms_gc=base_twms_gc,
                            base_twms_get_tile_service=base_twms_get_tile_service, wmts=wmts, twms=twms, xml=xml, sigevent_url=sigevent_url, debug=options.debug)