import pickle
import struct
import segment_bytes


class MotionSegment:
    def __init__(self, t_l, t_r, v_l, v_r):
        self.lin_target = t_l
        self.lin_velocity = v_l

        self.rot_target = t_r
        self.rot_velocity = v_r

    def encode(self):
        return struct.pack("dddd", self.lin_target, self.rot_target, self.lin_velocity, self.rot_velocity)
    
    @staticmethod
    def decode(b_data: bytes):
        t_l, t_r, v_l, v_r = struct.unpack("dddd", b_data)
        return MotionSegment(t_l, t_r, v_l, v_r)

class TargetMotionProfile:
    def __init__(self):
        self.__segments = []

    @staticmethod
    def from_segments(segments: list[MotionSegment]):
        profile = TargetMotionProfile()
        profile.set_segments(segments)
        return profile
    
    def set_segments(self, segments: list[MotionSegment]):
        self.__segments = segments

    def get_length(self):
        c_l, c_r = 0.0, 0.0
        c_t = 0.0
        for segment in self.__segments:
            len_l = abs((segment.lin_target - c_l) / segment.lin_velocity if segment.lin_velocity != 0 else 0.0)
            len_r = abs((segment.rot_target - c_r) / segment.rot_velocity if segment.rot_velocity != 0 else 0.0)

            c_l = segment.lin_target
            c_r = segment.rot_target
            len_t = max(len_l, len_r)
            c_t += len_t

        return c_t
    
    def move_len(self, segment_num: int):
        (start_l, start_r) = (self.__segments[segment_num - 1].lin_target, self.__segments[segment_num - 1].rot_target) if segment_num > 0 else (0.0, 0.0)

        end_l, end_r = self.__segments[segment_num].lin_target, self.__segments[segment_num].rot_target

        len_l = abs((end_l - start_l) / self.__segments[segment_num].lin_velocity if self.__segments[segment_num].lin_velocity != 0 else 0.0)
        len_r = abs((end_r - start_r) / self.__segments[segment_num].rot_velocity if self.__segments[segment_num].rot_velocity != 0 else 0.0)

        len_t = max(len_l, len_r)
        return len_t
    
    def time_at_posion(self, l_pos: float, r_pos: float, segment_num: int):
        (start_l, start_r) = (self.__segments[segment_num - 1].lin_target, self.__segments[segment_num - 1].rot_target) if segment_num > 0 else (0.0, 0.0)

        print("Current positions:", l_pos, r_pos)
        print("Start positions:", start_l, start_r)

        end_l, end_r = self.__segments[segment_num].lin_target, self.__segments[segment_num].rot_target

        print("End positions:", end_l, end_r)

        len_l = abs((end_l - start_l) / self.__segments[segment_num].lin_velocity if self.__segments[segment_num].lin_velocity != 0 else 0.0)
        len_r = abs((end_r - start_r) / self.__segments[segment_num].rot_velocity if self.__segments[segment_num].rot_velocity != 0 else 0.0)

        print("Segment lengths:", len_l, len_r)

        len_t = max(len_l, len_r)        

        frac_l = (l_pos - start_l) / (end_l - start_l) if end_l != start_l else 1.0

        frac_r = (r_pos - start_r) / (end_r - start_r) if end_r != start_r else 1.0
        
        print("Fractions:", frac_l, frac_r)

        frac_t = min(frac_l, frac_r)

        p_time =  frac_t * len_t + self.segment_begin_time(segment_num)

        assert p_time >= 0.0, "Calculated time is negative!"

        return p_time
    
    def time_until_end_at_position(self, l_pos: float, r_pos: float, segment_num: int):
        (start_l, start_r) = (self.__segments[segment_num - 1].lin_target, self.__segments[segment_num - 1].rot_target) if segment_num > 0 else (0.0, 0.0)

        end_l, end_r = self.__segments[segment_num].lin_target, self.__segments[segment_num].rot_target

        len_l = abs((end_l - start_l) / self.__segments[segment_num].lin_velocity if self.__segments[segment_num].lin_velocity != 0 else 0.0)
        len_r = abs((end_r - start_r) / self.__segments[segment_num].rot_velocity if self.__segments[segment_num].rot_velocity != 0 else 0.0)

        len_t = max(len_l, len_r)

        frac_l = (l_pos - start_l) / (end_l - start_l) if end_l != start_l else 1.0
        frac_r = (r_pos - start_r) / (end_r - start_r) if end_r != start_r else 1.0

        frac_t = min(frac_l, frac_r)
        return (1.0 - frac_t) * len_t
    
    def get_position_at_time(self, t: float):
        c_l, c_r = 0.0, 0.0
        c_t = 0.0
        for segment in self.__segments:
            len_l = abs((segment.lin_target - c_l) / segment.lin_velocity if segment.lin_velocity != 0 else 0.0)
            len_r = abs((segment.rot_target - c_r) / segment.rot_velocity if segment.rot_velocity != 0 else 0.0)

            len_t = max(len_l, len_r)

            if c_t + len_t > t:
                dt = t - c_t
                disp_l = segment.lin_velocity * dt
                disp_r = segment.rot_velocity * dt

                return c_l + disp_l, c_r + disp_r

            c_l = segment.lin_target
            c_r = segment.rot_target
            c_t += len_t

        return c_l, c_r
    
    def get_end_position(self):
        if not self.__segments:
            return 0.0, 0.0
        last_segment = self.__segments[-1]
        return last_segment.lin_target, last_segment.rot_target
    
    def get_bounds(self):
        max_l = 0.0
        max_r = 0.0

        for segment in self.__segments:
            target_l = segment.lin_target
            target_r = segment.rot_target

            max_l = max(max_l, target_l)
            max_r = max(max_r, target_r)

        return (max_l, max_r)
    
    def get_segment(self, index: int):
        return self.__segments[index]
    
    def num_segments(self):
        return len(self.__segments)
    
    def get_segments(self):
        return self.__segments.copy()
    
    def segment_at(self, time: float):
        c_t = 0.0
        cur_l, cur_r = 0.0, 0.0
        for i, segment in enumerate(self.__segments):
            len_l = abs((segment.lin_target - cur_l) / segment.lin_velocity if segment.lin_velocity != 0 else 0.0)
            len_r = abs((segment.rot_target - cur_r) / segment.rot_velocity if segment.rot_velocity != 0 else 0.0)

            len_t = max(len_l, len_r)

            cur_l = segment.lin_target
            cur_r = segment.rot_target

            if c_t + len_t >= (time - 1e-6):
                return i

            c_t += len_t

        return None
    
    def length_until_next_segment(self, time: float):
        c_t = 0.0
        cur_l, cur_r = 0.0, 0.0
        for segment in self.__segments:
            len_l = abs((segment.lin_target - cur_l) / segment.lin_velocity if segment.lin_velocity != 0 else 0.0)
            len_r = abs((segment.rot_target - cur_r) / segment.rot_velocity if segment.rot_velocity != 0 else 0.0)

            len_t = max(len_l, len_r)

            cur_l = segment.lin_target
            cur_r = segment.rot_target

            if c_t + len_t >= time:
                return (c_t + len_t) - time

            c_t += len_t

        return 0.0
    
    def segment_begin_time(self, segment_num: int):
        c_t = 0.0
        cur_l, cur_r = 0.0, 0.0
        for i in range(segment_num):
            segment = self.__segments[i]
            len_l = abs((segment.lin_target - cur_l) / segment.lin_velocity if segment.lin_velocity != 0 else 0.0)
            len_r = abs((segment.rot_target - cur_r) / segment.rot_velocity if segment.rot_velocity != 0 else 0.0)

            len_t = max(len_l, len_r)

            cur_l = segment.lin_target
            cur_r = segment.rot_target

            c_t += len_t

        return c_t
    
    def add_segment(self, segment: MotionSegment):
        self.__segments.append(segment)

    def encode(self):
        return pickle.dumps(self.__segments)
    
    @staticmethod
    def decode(b_data: bytes):
        return TargetMotionProfile.from_segments(pickle.loads(b_data))
    
class MotionState:
    def __init__(self, profile: TargetMotionProfile = None):
        self.__profile = profile
        self.__rep_amount = 0

        self.__current_position = 0.0
        self.__current_rep = 0

        self.__current_segment = 0

    def get_remaining_time(self):
        p_len = self.__profile.get_length()

        rem_time = p_len - self.__current_position
        return rem_time + ((self.__rep_amount - self.__current_rep) * p_len)
    
    def set_profile(self, profile: TargetMotionProfile):
        if profile.get_length() == 0:
            raise ValueError("Profile length cannot be zero!")
    
        self.__profile = profile

    def get_profile(self):
        return self.__profile
    
    def fill(self, max_l_size: float):
        l_size, r_size = self.__profile.get_bounds()
        self.__rep_amount = int(max_l_size // l_size)
    
    def resume_from(self, time: float):
        self.__current_position = time
        self.__current_rep = int(time // self.__profile.get_length())

        r_t = time % self.__profile.get_length()
        self.__current_segment = self.__profile.segment_at(r_t)
    
    def get_current_segment(self):
        return self.__current_segment
    
    def get_current_motion_command(self):
        print("Current segment:", self.__current_segment)
        seg = self.__profile.get_segment(self.__current_segment)

        return seg.lin_target + self.__current_rep * self.__profile.get_end_position()[0], seg.rot_target + self.__current_rep * self.__profile.get_end_position()[1], seg.lin_velocity, seg.rot_velocity

    def get_current_position(self):
        return self.get_position_at_time(self.__current_position + self.__profile.get_length() * self.__current_rep)
    
    def get_position_at_time(self, time: float):
        print("Current segment:", self.__current_segment)
        print("Current time:", time)

        reps = int(time // self.__profile.get_length())
        l_pos, r_pos = self.__profile.get_position_at_time(time % self.__profile.get_length())

        print("Current repetition:", reps)

        l, r = l_pos + reps * self.__profile.get_end_position()[0], r_pos + reps * self.__profile.get_end_position()[1]

        print("Calculated position:", l, r)

        return l, r

    def update_position(self, l_pos: float, r_pos: float):
        p_len = self.__profile.get_length()

        off_l, off_r = l_pos - self.__current_rep * self.__profile.get_end_position()[0], r_pos - self.__current_rep * self.__profile.get_end_position()[1]
        segment_time = self.__profile.time_at_posion(off_l, off_r, self.__current_segment)
        print("Segment time:", segment_time, "positions:", l_pos, r_pos, "current segment:", self.__current_segment)
        print("Offset positions:", off_l, off_r)
        print("Current position before update:", self.__current_position)

        if segment_time < p_len - 1e-3:
            self.__current_position = segment_time
            if self.get_time_until_end_of_segment() < 1e-3:
                self.finish_segment()
        else:
            self.finish_segment()

    def get_time_until_end_of_segment(self):
        p_time = (self.__current_position % self.__profile.get_length())
        seg_end_time = self.__profile.segment_begin_time(self.__current_segment) + self.__profile.move_len(self.__current_segment)

        return seg_end_time - p_time

    def finish_segment(self):
        self.__current_segment += 1

        if self.__current_segment >= self.__profile.num_segments():
            self.__current_segment = 0
            self.__current_rep += 1
            self.__current_position = 0

    def get_current_time(self):
        return self.__current_position + self.__current_rep * self.__profile.get_length()
    
    def reset(self):
        self.__current_position = 0.0
        self.__current_segment = 0
        self.__current_rep = 0

    def set_position(self, position: float):
        self.__current_position = position % self.__profile.get_length()
        self.__current_rep = int(position // self.__profile.get_length())

    def set_segment(self, segment: int):
        self.__current_segment = segment

    def set_rep_amount(self, rep_amount: int):
        self.__rep_amount = rep_amount

    def get_max_repetitions(self):
        return self.__rep_amount

    def get_current_repetition(self):
        return self.__current_rep

    def encode(self):
        b_my_data = segment_bytes.encode([struct.pack("d", self.__current_position), self.__current_segment.to_bytes(4, 'big'), self.__rep_amount.to_bytes(4, 'big')])
        b_profile = self.__profile.encode()

        return segment_bytes.encode([b_my_data, b_profile])
    
    @staticmethod
    def decode(b_data: bytes):
        segments = segment_bytes.decode(b_data)

        my_data = segment_bytes.decode(segments[0])
        profile = TargetMotionProfile.decode(segments[1])

        state = MotionState(profile)
        state.set_position(struct.unpack("d", my_data[0])[0])
        state.set_segment(int.from_bytes(my_data[1], 'big'))
        state.set_rep_amount(int.from_bytes(my_data[2], 'big'))

        return state
    
    def __str__(self):
        return f"MotionState(current_position={self.__current_position}, current_segment={self.__current_segment}, rep_amount={self.__rep_amount})" 
    
class TargetMotionConfig:
    def __init__(self, max_l_size: float):
        self.max_l_size = max_l_size

        self.traverse_speed_l = 2.0
        self.traverse_speed_r = 0.5

class TargetMotion:
    def __init__(self, config: TargetMotionConfig):
        pass

    def move_to_position(self, l_pos: float, r_pos: float, speed_l: float, speed_r: float):
        pass

    def get_position(self):
        pass
    
    def get_target_position(self):
        pass

    def set_move(self, do_move: bool):
        pass

    def home(self):
        pass

    def is_homing(self):
        pass
