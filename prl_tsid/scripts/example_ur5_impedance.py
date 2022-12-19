#!/usr/bin/env python

# roslaunch prl_ur5_run real.launch velocity_control:=true ff_control:=true moveit:=false sensors:=true enable_right_camera:=false
# roslaunch ros_cosypose singleview_loop.launch bringup_camera:=false dataset:=ycbv debug:=true camera_name:=left_camera/color detection_threshold:=0.85

from prl_tsid.commander import PathFollower
import numpy as np
import pinocchio as pin

import rospy
rospy.init_node("TSID_example", anonymous=True)
rospy.loginfo("init_node")

# Plan a trajectory using HPP
from prl_hpp.ur5 import planner, robot, commander_left_arm, commander_right_arm
pi = 3.1415926

# Start the commanders
commander_left_arm.start_fwd()

planner.lock_grippers()
planner.lock_right_arm()
planner.set_velocity_limit(0.25)
planner.set_acceleration_limit(0.25)

# Exectute the trajectories using TSID
pf = PathFollower(robot)
# pf.set_velocity_limit(1)
# pf.set_acceleration_limit(1)
# pf.set_torque_limit(1)

import tf
from geometry_msgs.msg import WrenchStamped
from sensor_msgs.msg import Joy

tf_listener = tf.TransformListener()

# All this part is erroneous in most case #
# Temporary fix until pin is released #
def is_frame_supported(model, parent_frame_id, child_frame_id):
    current_id = model.frames[child_frame_id].previousFrame
    while model.frames[parent_frame_id].parent == model.frames[current_id].parent and  current_id > parent_frame_id:
        current_id = model.frames[current_id].previousFrame
    return model.frames[parent_frame_id].parent == model.frames[current_id].parent and current_id == parent_frame_id

def compute_supported_frames_in_body(model, parent_frame_id):
    res = []
    for i,f in enumerate(model.frames[parent_frame_id:], start=parent_frame_id):
        if(is_frame_supported(model, parent_frame_id, i)):
            res.append(i)
    return res

def compute_supported_inertia_in_body(model, frame_id):
    supported_frames = [frame_id] + compute_supported_frames_in_body(model,frame_id)
    j_inertia = pin.Inertia(0, np.zeros(3), np.zeros([3,3]))
    for f_id in supported_frames:
        jMf = model.frames[f_id].placement
        j_inertia += jMf * model.frames[f_id].inertia # Compute the inertia in the joint frame
    jMf = model.frames[frame_id].placement
    return jMf.actInv(j_inertia)

def compute_supported_effort(model, data, frame_id):
    joint_id = model.frames[frame_id].parent
    iMf = model.frames[frame_id].placement
    oMf = data.oMi[joint_id] * iMf
    inertia = compute_supported_inertia_in_body(model, frame_id)
    v_frame = pin.getFrameVelocity(model, data, frame_id, pin.ReferenceFrame.LOCAL)
    a_frame = pin.getFrameAcceleration(model, data, frame_id, pin.ReferenceFrame.LOCAL)
    effort = inertia.vxiv(v_frame) + inertia * (a_frame - oMf.actInv(model.gravity))
    effort = iMf.act(effort)
    for j_id in model.subtrees[joint_id][1:]:
        if model.parents[j_id] != joint_id:
            continue
        effort += data.liMi[j_id].act(data.f[j_id])
    return iMf.actInv(effort)
###################################


FILT_WIN = int(1 / 0.01) # filter window =  1s @ 100Hz
filt_list = [pin.Force(np.zeros(6)) for _ in range(FILT_WIN)]
filt_i = 0
filt_avg = pin.Force(np.zeros(6))
def control_from_fts_cb(msg):
    global FILT_WIN, filt_list, filt_i, filt_avg
    force_trans_fric = 3 # The first 3 N won't be counted
    force_ros_fric = 0.2 # The first 0.2 Nm won't be counted
    max_trans_vel = 0.2
    max_rot_vel = np.pi/2

    answer = WrenchStamped(header = msg.header)

    # # Already computed internally of the F/T sensor
    # frame_id = robot.pin_robot_wrapper.model.getFrameId("left_measurment_joint")
    #
    # try:
    #     t, q, v, tau = robot.get_meas_qvtau()
    # except:
    #     rospy.logwarn("Joint out of bounds send 0 velocity.")
    #     answer = WrenchStamped(header = msg.header)
    #     pub.publish(answer)
    #     return
    #
    # q, v, tau = np.array(q), np.array(v), np.array(tau)
    # robot.pin_robot_wrapper.forwardKinematics(q, v)
    # pin.aba(robot.pin_robot_wrapper.model, robot.pin_robot_wrapper.data, q, v, tau)
    # pin.crba(robot.pin_robot_wrapper.model, robot.pin_robot_wrapper.data, q)
    # effort = compute_supported_effort(robot.pin_robot_wrapper.model, robot.pin_robot_wrapper.data, frame_id)
    #
    # wrist_f_tot = pin.Force(np.array([msg.wrench.force.x, msg.wrench.force.y, msg.wrench.force.z, msg.wrench.torque.x, msg.wrench.torque.y, msg.wrench.torque.z]))
    # diff_f = effort + wrist_f_tot

    wrist_f = pin.Force(np.array([msg.wrench.force.x, msg.wrench.force.y, msg.wrench.force.z, msg.wrench.torque.x, msg.wrench.torque.y, msg.wrench.torque.z]))
    filt_avg -= filt_list[filt_i]
    filt_list[filt_i] = wrist_f / FILT_WIN
    filt_avg += filt_list[filt_i]
    filt_i = (filt_i + 1) % FILT_WIN

    # Add "friction" to the measured force
    res_fric = np.zeros(6)
    for i in range(6):
        fric = force_trans_fric if i<3 else force_ros_fric
        if(abs(filt_avg.vector[i]) < fric):
            continue
        res_fric[i] = filt_avg.vector[i] - np.sign(filt_avg.vector[i]) * fric

    answer.wrench.force.x = res_fric[0]
    answer.wrench.force.y = res_fric[1]
    answer.wrench.force.z = res_fric[2]
    answer.wrench.torque.x = res_fric[3]
    answer.wrench.torque.y = res_fric[4]
    answer.wrench.torque.z = res_fric[5]

    pub.publish(answer)

    # v_z = np.clip(vz, -max_trans_vel, max_trans_vel)

def control_from_joy_cb(msg):
    max_trans_vel = 0.2
    max_rot_vel = np.pi/2

    vel_loc = pin.Motion(np.array([max_trans_vel*msg.axes[6], max_trans_vel*msg.axes[7], max_trans_vel*msg.axes[1], 0, 0, max_rot_vel*msg.axes[3]]))


    try:
        _, q, _, _ = robot.get_meas_qvtau()
    except:
        rospy.logwarn("Joint out of bounds send 0 velocity.")
        pf.eeVelSample.derivative(np.zeros(6))
        return

    frame_id = robot.pin_robot_wrapper.model.getFrameId("left_measurment_joint")
    oMf = robot.pin_robot_wrapper.framePlacement(np.array(q), frame_id)
    oMf_rot = pin.SE3(oMf.rotation, np.zeros(3))
    vel_glob = oMf_rot.act(vel_loc)

    pf.eeVelSample.derivative(vel_glob.vector)

if __name__=='__main__':
    pub = rospy.Publisher("/left_ft_wrench_error", WrenchStamped, queue_size=0)
    rospy.Subscriber("/left_ft_wrench", WrenchStamped, control_from_fts_cb)
    rospy.Subscriber("/joy", Joy, control_from_joy_cb)

    # start_pose = [[-0.6, 0.0, 0.05], [np.pi, 0, np.pi]]
    # path = planner.make_gripper_approach(robot.left_gripper_name, start_pose, approach_distance = 0.01)

    # input("Press enter to execute initial motion...")
    # pf.execute_path(path, [commander_left_arm], 0.1, velocity_ctrl = True)

    # robot.remove_collision_pair("table_link_0", "left_gripper_finger_1_finger_tip_0")
    # robot.remove_collision_pair("table_link_0", "left_gripper_finger_1_finger_tip_1")
    # robot.remove_collision_pair("table_link_0", "left_gripper_finger_1_flex_finger_0")
    # robot.remove_collision_pair("table_link_0", "left_gripper_finger_2_finger_tip_0")
    # robot.remove_collision_pair("table_link_0", "left_gripper_finger_2_finger_tip_1")
    # robot.remove_collision_pair("table_link_0", "left_gripper_finger_2_flex_finger_0")

    input("Press enter to execute TSID motion...")
    pf.follow_velocity("left_gripper_grasp_frame", [commander_left_arm], 0.1, velocity_ctrl = True)

    input("Press enter to quit...")