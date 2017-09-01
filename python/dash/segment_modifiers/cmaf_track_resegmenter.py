"""Resegment a CMAF track to new exact average duration.

Useful to get audio segments with a specified average duration."""


# The copyright in this software is being made available under the BSD License,
# included below. This software may be subject to other third party and contributor
# rights, including patent rights, and no such rights are granted under this license.
#
# Copyright (c) 2016, Dash Industry Forum.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without modification,
# are permitted provided that the following conditions are met:
#  * Redistributions of source code must retain the above copyright notice, this
#  list of conditions and the following disclaimer.
#  * Redistributions in binary form must reproduce the above copyright notice,
#  this list of conditions and the following disclaimer in the documentation and/or
#  other materials provided with the distribution.
#  * Neither the name of Dash Industry Forum nor the names of its
#  contributors may be used to endorse or promote products derived from this software
#  without specific prior written permission.
#
#  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS AS IS AND ANY
#  EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
#  WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED.
#  IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT,
#  INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT
#  NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR
#  PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY,
#  WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
#  ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
#  POSSIBILITY OF SUCH DAMAGE.

from argparse import ArgumentParser
from collections import namedtuple

from structops import str_to_uint16, uint16_to_str, uint32_to_str
from structops import str_to_uint32, str_to_uint64, uint64_to_str
from mp4filter import MP4Filter


SegmentData = namedtuple("SegmentData", "nr start dur size data")
SampleData = namedtuple("SampleData", "start dur size offset flags cto")
SegmentInfo = namedtuple("SegmentInfo", "start_nr end_nr start_time dur")



class SampleMetadataExtraction(MP4Filter):
    "Extract sample information from a CMAF track file."

    def __init__(self, file_name, verbose=False):
        super(SampleMetadataExtraction, self).__init__(file_name)
        self.verbose = verbose
        self.relevant_boxes = ["moov", "moof", "sidx"]
        self.track_timescale = None
        self.default_sample_duration = None
        self.default_sample_cto = None
        self.default_sample_flags = None
        self.default_sample_size = None
        self.input_segments = []
        self.samples = []
        self.last_moof_start = 0
        self.styp = ""  # styp box, if any
        self.sidx_start = ""  # The sidx startdata before reference_count
        self.tfhd = ""
        self.tfdt_size = None
        self.trun_base_size = None  # Base size for trun
        self.trun_sample_flags = None  # Sample flags

    def filterbox(self, box_type, data, file_pos, path=""):
        "Filter box or tree of boxes recursively."
        containers = ("moov", "moov.trak", "moov.trak.mdia", "moov.mvex",
                      "moof", "moof.traf")
        if path == "":
            path = box_type
        else:
            path = "%s.%s" % (path, box_type)
        output = ""
        if path == "emsg":
            ValueError("emsg not supported")
        if path == "styp":
            self.styp = data
        if path in containers:
            if path == "moof":
                self.last_moof_start = file_pos
            output += data[:8]
            pos = 8
            while pos < len(data):
                size, box_type = self.check_box(data[pos:pos + 8])
                output += self.filterbox(box_type, data[pos:pos+size],
                                         file_pos + len(output), path)
                pos += size
        elif path == "moov.mvex.trex":
            output = self.process_trex(data)
        elif path == "moov.trak.mdia.mdhd":
            output = self.process_mdhd(data)
        elif path == "moof.mfhd":
            output = self.process_mfhd(data)
        elif path == "moof.traf.tfhd":
            output = self.process_tfhd(data)
        elif path == "moof.traf.tfdt":
                output = self.process_tfdt(data)
        elif path == "moof.traf.trun":
            output = self.process_trun(data)
        elif path == "sidx":
            output = self.process_sidx(data)
        else:
            output = data
        return output

    def process_trex(self, data):
        "Get potential default values."
        self.track_id = str_to_uint32(data[12:16])
        self.default_sample_description_index = str_to_uint32(data[16:20])
        self.default_sample_duration = str_to_uint32(data[20:24])
        self.default_sample_size = str_to_uint32(data[24:28])
        self.default_sample_flags = str_to_uint32(data[28:32])
        return data

    def process_mdhd(self, data):
        "Extract track timescale."
        version = ord(data[8])
        if version == 1:
            offset = 28
        else:
            offset = 20
        self.track_timescale = str_to_uint32(data[offset:offset+4])
        return data

    def process_sidx(self, data):
        "Extract sidx parts."
        version = ord(data[8])
        if version == 0:
            first_offset = str_to_uint32(data[24:28])
            pos = 28
        else:
            first_offset = str_to_uint64(data[28:36])
            pos = 36
        if first_offset != 0:
            raise ValueError("Only supports first_offset == 0")
        pos += 2
        self.sidx_start = data[:pos]  # Up until reference_count
        reference_count = str_to_uint16(data[pos:pos+2])
        pos += 2
        for i in range(reference_count):
            field = str_to_uint32(data[pos:pos+4])
            pos += 4
            reference_type = field >> 31
            if reference_type != 0:
                raise ValueError("Only sidx reference type == 0 supported")
            size = field & 0x7fffffff
            duration = str_to_uint32(data[pos:pos+4])
            if self.verbose:
                print("Input sidx %d: dur=%d" % (i + 1, duration))
            pos += 4
            field = str_to_uint32(data[pos:pos+4])
            pos += 4
            starts_with_sap = field >> 31
            if starts_with_sap != 1:
                raise ValueError("Only sidx with starts_with_sap supported")
            sap_type = (field >> 28) & 0x7
            if sap_type != 1:
                raise ValueError("Only sap type 1 supported, not %d" %
                                 sap_type)
            sap_delta_time = field & 0x0fffffff
            if sap_delta_time != 0:
                raise ValueError("Only sap_delta_time == 0 supported")
        return  data

    def process_mfhd(self, data):
        "Extract sequence number."
        sequence_number = str_to_uint32(data[12:16])
        segment = {'sequence_number': sequence_number,
                   'moof_start_offset': self.last_moof_start}
        self.input_segments.append(segment)
        return data

    def process_tfhd(self, data):
        "Check flags and set default values."
        flags = str_to_uint32(data[12:16]) & 0xffffff
        pos = 12
        if flags & 0x0001:  # has_data_offset
            data_offset = str_to_uint32(data[pos:pos+4])
            pos += 4
        if flags & 0x0004:  # first_sample_flags
            self.first_sample_flags = str_to_uint32(data[pos:pos+4])
            pos +=4
        if flags & 0x0100:  # sample_duration
            self.default_sample_duration = str_to_uint32(data[pos:pos+4])
            pos += 4
        if flags & 0x0200:  # sample_size
            self.default_sample_size = str_to_uint32(data[pos:pos+4])
            pos += 4
        if flags & 0x0400:  # sample_flags
            self.default_sample_flags = str_to_uint32(data[pos:pos+4])
            pos += 4
        if flags & 0x0800:  # sample_composition_time_offset
            self.default_sample_cto = str_to_uint32(data[pos:pos+4])
            pos += 4
        self.tfhd = data
        return data

    def process_tfdt(self, data):
        "Extract baseMediaDecodeTime."
        version = ord(data[8])
        if version == 0:
            self.base_media_decode_time = str_to_uint32(data[12:16])
        else:
            self.base_media_decode_time = str_to_uint64(data[12:20])
        seg =  self.input_segments[-1]
        seg['base_media_decode_time'] = self.base_media_decode_time
        self.tfdt_size = len(data)
        return data

    def process_trun(self, data):
        """Adjust time of tfdt if offset set."""
        version_and_flags = str_to_uint32(data[8:12])
        # version = version_and_flags >> 24
        flags = version_and_flags & 0xffffff
        sample_count = str_to_uint32(data[12:16])
        pos = 16
        start = self.base_media_decode_time
        data_offset = self.last_moof_start
        if flags & 0x1:  # data_offset_present
            data_offset += str_to_uint32(data[pos:pos+4])
            pos += 4
        else:
            raise ValueError("Cannot handle case without data_offset")
        if flags & 0x4:  # first_sample_flags
            pos += 4
            raise ValueError("Cannot handle first_sample_flag")
        self.trun_base_size = pos  # How many bytes this far
        self.trun_sample_flags = flags
        for i in range(sample_count):
            sample_duration = self.default_sample_duration
            sample_size = self.default_sample_size
            sample_flags = self.default_sample_flags
            cto = self.default_sample_cto
            if flags & 0x100:  # sample_duration present
                sample_duration = str_to_uint32(data[pos:pos + 4])
                pos += 4
            if flags & 0x200:  # sample_size present
                sample_size = str_to_uint32(data[pos:pos + 4])
                pos += 4
            if flags & 0x400:  # sample_flags present
                sample_flags = str_to_uint32(data[pos:pos + 4])
                pos += 4
            if flags & 0x800:  # composition_time_offset present
                cto = str_to_uint32(data[pos:pos + 4])
                pos += 4
            sample = SampleData(start, sample_duration, sample_size,
                                data_offset, sample_flags, cto)
            self.samples.append(sample)
            start += sample_duration
            data_offset += sample_size
        seg = self.input_segments[-1]
        seg['duration'] = start - self.base_media_decode_time
        return data

    def find_header_end(self):
        "Find where the header ends. This part will be left untouched."
        header_end = 0
        for size, box in self.top_level_boxes:
            if box in ('sidx', 'styp', 'moof'):
                break
            header_end += size
        return header_end

    def bytes_of_trun_sample_data(self):
        "Return nr bytes per sample in trun table depedning on flags."
        nr = 0
        if self.trun_sample_flags & 0x100:
            nr += 4
        if self.trun_sample_flags & 0x200:
            nr += 4
        if self.trun_sample_flags & 0x400:
            nr += 4
        if self.trun_sample_flags & 0x800:
            nr += 4
        return nr

    def construct_new_mdat(self, media_info):
        "Return an mdat box with data for samples in media_info."
        start_nr = media_info.start_nr
        end_nr = media_info.end_nr
        sample_data = []
        for i in range(media_info.start_nr, media_info.end_nr):
            sample = self.samples[i]
            sample_data.append(self.data[sample.offset:sample.offset +
                                                       sample.size])
        combined_data = "".join(sample_data)
        return uint32_to_str(8 + len(combined_data)) + 'mdat' + combined_data


class Resegmenter(object):
    "Resegment a CMAF track into a new output track."

    def __init__(self, input_file, duration_ms, output_file, verbose):
        self.input_file = input_file
        self.duration_ms = duration_ms
        self.output_file = output_file
        self.verbose = verbose
        self.input_parser = None

    def resegment(self):
        "Resegment the track with new duration."
        self.input_parser = SampleMetadataExtraction(self.input_file,
                                                     self.verbose)
        ip = self.input_parser
        ip.filter_top_boxes()
        if self.verbose:
            for i, segment in enumerate(ip.input_segments):
                print("Input segment %d: dur=%d" % (i + 1,
                                                    segment['duration']))
        input_header_end = self.input_parser.find_header_end()
        output = ip.data[:input_header_end]
        segment_info = self._map_samples_to_new_segments()
        segment_sizes = self. _calculate_segment_sizes(segment_info)
        if self.input_parser.sidx_start:
            output += self._generate_sidx(segment_info, segment_sizes)
        for i, seg_info in enumerate(segment_info):
            if ip.styp:
                output += ip.styp
            output += self._generate_moof(i+1, seg_info)
            output += ip.construct_new_mdat(seg_info)
        if self.output_file:
            with open(self.output_file, "wb") as ofh:
                ofh.write(output)

    def _map_samples_to_new_segments(self):
        "Calculate which samples go into which segments."
        new_segment_info = []
        segment_nr = 1
        acc_time = 0
        start_nr = 0
        start_sample = self.input_parser.samples[0]
        timescale = self.input_parser.track_timescale
        nr_samples = len(self.input_parser.samples)
        for i, sample in enumerate(self.input_parser.samples):
            acc_time += sample.dur
            if acc_time * 1000  > segment_nr * self.duration_ms * timescale:
                end_sample = self.input_parser.samples[i-1]
                end_time = end_sample.start + end_sample.dur
                seg_dur = end_time - start_sample.start
                info = SegmentInfo(start_nr, i, start_sample.start, seg_dur)
                new_segment_info.append(info)
                start_nr = i
                start_sample = sample
                segment_nr += 1

        if start_nr != nr_samples - 1:
            end_sample = self.input_parser.samples[-1]
            end_time = end_sample.start + end_sample.dur
            info = SegmentInfo(start_nr, nr_samples, start_sample.start,
                               end_time - start_sample.start)
            new_segment_info.append(info)
        if self.verbose:
            for i, info in enumerate(new_segment_info):
                print("Output segment %d: dur=%d" % (i +1, info.dur))
        print("Generating %d segments from %d" %
              (len(new_segment_info),  len(self.input_parser.input_segments)))
        return new_segment_info

    def _calculate_traf_size(self, nr_samples):
        ip = self.input_parser
        moof_size = 8 + 16 + ip.tfdt_size + ip.trun_base_size
        moof_size += nr_samples * ip.bytes_of_trun_sample_data()
        return moof_size

    def _calculate_moof_size(self, nr_samples):
        return 8 + 16 + self._calculate_traf_size(nr_samples)

    def _calculate_segment_sizes(self, segment_info):
        "Calculate the size of every segment (for sidx)."
        sizes = []
        ip = self.input_parser
        for info in segment_info:
            styp_size = len(ip.styp)
            moof_size = self._calculate_moof_size(info.end_nr - info.start_nr)
            mdat_size = 8
            for sample in ip.samples[info.start_nr:info.end_nr]:
                mdat_size += sample.size
            seg_size = styp_size + moof_size + mdat_size
            sizes.append(seg_size)
        return sizes

    def _generate_sidx(self, segment_info, segment_sizes):
        "Generate updated sidx box."
        output = self.input_parser.sidx_start
        output += uint16_to_str(len(segment_sizes))
        for info, size in zip(segment_info, segment_sizes):
            output += uint32_to_str(size)  # Setting reference type to 0
            output += uint32_to_str(info.dur)
            output += uint32_to_str(0x90000000)
        size = len(output)
        output = uint32_to_str(size) + output[4:]
        return output

    def _generate_moof(self, sequence_nr, seg_info):
        "Generate a moof box with the correct sample entries"
        ip = self.input_parser
        nr_samples = seg_info.end_nr - seg_info.start_nr
        moof_size = self._calculate_moof_size(nr_samples)
        output = uint32_to_str(moof_size) + 'moof'
        output += (uint32_to_str(16) + 'mfhd' + uint32_to_str(0) +
                   uint32_to_str(sequence_nr))
        traf_size = self._calculate_traf_size(nr_samples)
        output += uint32_to_str(traf_size) + 'traf'
        output += ip.tfhd
        output += uint32_to_str(ip.tfdt_size) + 'tfdt'
        if ip.tfdt_size == 16:
            output += uint32_to_str(0x00000000) + uint32_to_str(
                seg_info.start_time)
        else:
            output += uint32_to_str(0x01000000) + uint64_to_str(
                seg_info.start_time)
        output += self._generate_trun_box(seg_info, moof_size)
        return output

    def _generate_trun_box(self, seg_info, moof_size, version=0):
        "Generate trun box with correct sample data for segment."
        ip = self.input_parser
        sample_flags = ip.trun_sample_flags
        sample_data_size = ip.bytes_of_trun_sample_data()
        sample_count = seg_info.end_nr - seg_info.start_nr
        trun_size = 20 + sample_count * sample_data_size
        output = uint32_to_str(trun_size) + 'trun'
        version_and_flags = (version << 24) | sample_flags
        output += uint32_to_str(version_and_flags)
        output += uint32_to_str(sample_count)
        output += uint32_to_str(moof_size + 8) # 8 bytes into mdat
        for sample in ip.samples[seg_info.start_nr:seg_info.end_nr]:
            if sample_flags & 0x100:
                output += uint32_to_str(sample.dur)
            if sample_flags & 0x200:
                output += uint32_to_str(sample.size)
            if sample_flags & 0x400:
                output += uint32_to_str(sample.flags)
            if sample_flags & 0x800:
                output += uint32_to_str(sample.cto)
        return output


def main():
    parser = ArgumentParser(usage="usage: %(prog)s [options]")

    parser.add_argument("-i", "--input-file",
                        action="store",
                        dest="input_file",
                        default="",
                        help="Input CMAF track file",
                        required=True)

    parser.add_argument("-d", "--duration",
                        action="store",
                        dest="duration",
                        type=float,
                        default=2000,
                        help="New average segment duration in milliseconds")

    parser.add_argument("-o", "--output-file",
                        action="store",
                        dest="output_file",
                        default="",
                        help="Output CMAF track file")

    parser.add_argument("-v", "--verbose",
                        action="store_true",
                        dest="verbose",
                        help="Verbose mode")

    args = parser.parse_args()

    resegmenter = Resegmenter(args.input_file, args.duration,
                              args.output_file,
                              args.verbose)
    resegmenter.resegment()

if __name__ == "__main__":
    main()
